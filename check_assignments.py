"""Moodleのタイムライン（未提出の課題・小テスト）を取得し、締め切りが近いものをDiscordに通知する。

設定は config.json で行う（README参照）:
  moodle_url     自分の大学のMoodleのURL（例: https://moodle.example.ac.jp）
  default_days   締め切りの何日前から通知するか
  default_hours  何時に通知するか（例: [9, 21]）
  courses        授業名（の一部）ごとの設定
                   数字            … 日数だけ変える
                   {"days": 2, "hours": [21]} … 日数と時間を変える
                   "off"           … その授業は通知しない

環境変数（GitHubのSecretsで設定）:
  MOODLE_USERNAME      ログインID
  MOODLE_PASSWORD      パスワード
  DISCORD_WEBHOOK_URL  通知先（未設定なら画面に表示するだけ＝PCでのテスト用）
  MOODLE_URL           config.json の moodle_url より優先したいときだけ
  FORCE_ALL            true なら時間の設定を無視してすぐ通知（手動実行用）
  DAYS_AHEAD           指定すると日数の設定を無視して全授業この日数で見る（テスト用）
  CONFIG_URL / CONFIG_SECRET  設定を外部（Discordコマンド用Worker）から読む場合だけ
"""

import getpass
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"
CONFIG_PATH = Path(__file__).with_name("config.json")

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DAYS_OVERRIDE = os.environ.get("DAYS_AHEAD", "").strip()
FORCE_ALL = os.environ.get("FORCE_ALL", "").strip().lower() == "true"
CONFIG_URL = os.environ.get("CONFIG_URL", "").strip().rstrip("/")
CONFIG_SECRET = os.environ.get("CONFIG_SECRET", "")
BASE = ""  # main() で決める


# ---------- 設定 ----------

def load_config():
    """設定を読み込む。外部の設定（Worker）があればそれを優先し、だめなら config.json。"""
    config = {"default_days": 3, "default_hours": [9, 21], "courses": {}}
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if CONFIG_URL:
        try:
            res = requests.get(f"{CONFIG_URL}/config", headers=_api_headers(), timeout=15)
            res.raise_for_status()
            config.update(res.json())
        except Exception as e:
            print("外部の設定を読めなかったので config.json を使います:", e)
    return config


def _is_off(value):
    return str(value).strip().lower() in ("off", "false", "none", "通知しない")


def _hours(value):
    """[9, "21:00"] のような指定を {9, 21} にする。"""
    if not isinstance(value, list):
        value = [value]
    hours = set()
    for v in value:
        h = int(str(v).split(":")[0])
        if not 0 <= h <= 23:
            raise ValueError(f"時間は0〜23で指定してください: {v}")
        hours.add(h)
    return hours


def rule_for(course_name, config):
    """その授業の (日数, 通知する時間の集合) を返す。通知しない授業は None。"""
    days = float(config.get("default_days", 3))
    hours = _hours(config.get("default_hours", [9, 21]))
    for key, value in config.get("courses", {}).items():
        if not key or key not in course_name:
            continue
        if _is_off(value):
            return None
        if isinstance(value, dict):
            if value.get("off") is True or _is_off(value.get("days", "")):
                return None
            days = float(value.get("days", days))
            hours = _hours(value["hours"]) if "hours" in value else hours
        else:
            days = float(value)
        break
    if DAYS_OVERRIDE:
        days = float(DAYS_OVERRIDE)
    return days, hours


def all_rules(config):
    """設定に出てくるすべての (日数, 時間) と、基本設定。"""
    rules = [rule_for("", config)]
    for key in config.get("courses", {}):
        r = rule_for(key, config)
        if r:
            rules.append(r)
    return rules


# ---------- Moodle ----------

def login(s, username, password):
    """Moodleにログインし、sesskeyを返す。"""
    page = s.get(f"{BASE}/login/index.php", timeout=30)
    m = re.search(r'name="logintoken"\s+value="([^"]+)"', page.text)
    data = {"username": username, "password": password}
    if m:
        data["logintoken"] = m.group(1)
    s.post(f"{BASE}/login/index.php", data=data, timeout=30)

    home = s.get(f"{BASE}/my/", timeout=30)
    m = re.search(r'"sesskey":"([^"]+)"', home.text)
    if "/login/" in home.url or not m:
        raise RuntimeError("ログインに失敗しました（ユーザー名・パスワードを確認してください）")
    return m.group(1)


def ajax(s, sesskey, method, args):
    payload = [{"index": 0, "methodname": method, "args": args}]
    res = s.post(f"{BASE}/lib/ajax/service.php", params={"sesskey": sesskey, "info": method},
                 json=payload, timeout=30)
    res.raise_for_status()
    result = res.json()[0]
    if result.get("error"):
        raise RuntimeError(f"{method} に失敗: {result.get('exception', {}).get('message')}")
    return result["data"]


def get_timeline(s, sesskey, time_from, time_to):
    """ダッシュボードのタイムラインと同じデータを取得する。"""
    data = ajax(s, sesskey, "core_calendar_get_action_events_by_timesort", {
        "limitnum": 50,
        "timesortfrom": time_from,
        "timesortto": time_to,
        "limittononsuspendedevents": True,
    })
    return data["events"]


def _api_headers():
    return {"Authorization": f"Bearer {CONFIG_SECRET}"}


def report_courses(s, sesskey, events):
    """外部の設定（Worker）を使うときだけ、授業名の候補を送る。"""
    if not CONFIG_URL:
        return
    names = []
    try:
        data = ajax(s, sesskey, "core_course_get_enrolled_courses_by_timeline_classification",
                    {"classification": "inprogress", "limit": 0, "offset": 0, "sort": "fullname"})
        names = [c["fullname"] for c in data["courses"]]
    except Exception as e:
        print("授業一覧の取得に失敗:", e)
    names += [(ev.get("course") or {}).get("fullname", "") for ev in events]
    try:
        requests.post(f"{CONFIG_URL}/courses", headers=_api_headers(),
                      json=sorted({n for n in names if n}), timeout=15).raise_for_status()
    except Exception as e:
        print("授業一覧の送信に失敗:", e)


# ---------- 通知 ----------

def select_todo(events, now, hour, config):
    """未提出で、授業ごとの日数・時間の設定に合うものだけを返す。"""
    todo = []
    for e in events:
        if not (e.get("action") or {}).get("actionable"):
            continue  # 提出済み、または操作不要
        course = (e.get("course") or {}).get("fullname", "")
        rule = rule_for(course, config)
        if rule is None:
            continue  # 通知しない授業
        days, hours = rule
        if not FORCE_ALL and hour not in hours:
            continue  # この時間には通知しない授業
        if e["timesort"] <= now + days * 86400:
            todo.append(e)
    return sorted(todo, key=lambda e: e["timesort"])


def fmt_time(ts):
    d = datetime.fromtimestamp(ts, JST)
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]}) {d:%H:%M}"


def build_message(todo, now):
    lines = [f"⚠️ **未提出が {len(todo)} 件あります**"]
    for e in todo:
        due = e["timesort"]
        course = (e.get("course") or {}).get("fullname", "")
        hours = (due - now) // 3600
        left = "**期限切れ**" if hours < 0 else f"あと約{hours}時間"
        m = re.fullmatch(r"「(.+)」の提出期限", e["name"])
        name = m.group(1) if m else e["name"]
        lines.append(f"・**{name}**\n　{course}\n　締切 {fmt_time(due)}・{left}\n　<{e.get('url', '')}>")
    return "\n".join(lines)


def notify(text):
    if not WEBHOOK_URL:
        print(text)
        return
    for i in range(0, len(text), 1900):
        requests.post(WEBHOOK_URL, json={"content": text[i:i + 1900]}, timeout=30).raise_for_status()


def main():
    global BASE
    config = load_config()
    BASE = (os.environ.get("MOODLE_URL") or config.get("moodle_url") or "").strip().rstrip("/")
    if not BASE or "example.ac.jp" in BASE:
        raise RuntimeError("config.json の moodle_url に自分の大学の Moodle の URL を設定してください")

    now = int(time.time())
    hour = datetime.fromtimestamp(now, JST).hour
    rules = all_rules(config)

    # この時間に通知する授業がひとつもなければ、ログインせずに終わる
    if not FORCE_ALL and not any(hour in hours for _, hours in rules):
        print(f"{hour}時は通知する設定の授業がないので終了します")
        return

    username = os.environ.get("MOODLE_USERNAME") or input("ユーザー名: ").strip()
    password = os.environ.get("MOODLE_PASSWORD") or getpass.getpass("パスワード（表示されません）: ")
    max_days = max(days for days, _ in rules)

    with requests.Session() as s:
        s.headers["User-Agent"] = "Mozilla/5.0 (moodle-deadline-notifier)"
        sesskey = login(s, username, password)
        # 過去7日（期限切れでまだ出せるもの）〜 いちばん長い通知日数
        events = get_timeline(s, sesskey, now - 7 * 86400, now + int(max_days * 86400))
        report_courses(s, sesskey, events)

    todo = select_todo(events, now, hour, config)
    if not todo:
        print("通知する未提出の課題はありません")
        return
    notify(build_message(todo, now))
    print(f"{len(todo)} 件を通知しました")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            if WEBHOOK_URL:
                notify(f"❌ 課題チェックでエラー: {e}")
        finally:
            print("エラー:", e, file=sys.stderr)
            sys.exit(1)
