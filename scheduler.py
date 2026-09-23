#!/usr/bin/env python3
"""Generate a fair festival duty roster with OR-Tools CP-SAT."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model


ROOT = Path(__file__).resolve().parent
PEOPLE_PATH = ROOT / "docs" / "mamba-performance.json"
LINEUP_PATH = ROOT / "docs" / "festival-performances.json"
OUTPUT_PATH = ROOT / "docs" / "schedule-v3.json"
REPORT_PATH = ROOT / "docs" / "schedule-report-v3.md"
V2_OUTPUT_PATH = ROOT / "docs" / "schedule-output.json"
WINDOW_START = "12:00"
WINDOW_END = "21:30"
WINDOW_MINUTES = 570
MIN_SHIFT = 40
MAX_SHIFT = 120
MIN_BREAK = 40
RATING_CAPS = {"must_see": 3, "very_want_to_see": 4, "somewhat_want_to_see": 2}
RATING_POINTS = {"must_see": 100, "very_want_to_see": 30, "somewhat_want_to_see": 10}
PREFERENCE_LABELS = {
    "must_see": "必须看",
    "very_want_to_see": "特别想看",
    "somewhat_want_to_see": "有点想看",
}
SAT_SCALE = 10_000
MAX_HOURS_SPREAD = 180


def minute_of_day(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def clock_time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def read_inputs(people_path: Path, lineup_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    people = json.loads(people_path.read_text(encoding="utf-8"))["people"]
    performances = json.loads(lineup_path.read_text(encoding="utf-8"))["performances"]
    warnings: list[str] = []

    ids: set[str] = set()
    for performance in performances:
        performance["start_minute"] = minute_of_day(performance["startTime"])
        performance["end_minute"] = minute_of_day(performance["endTime"])
        ids.add(performance["date"])
        if performance["end_minute"] <= performance["start_minute"]:
            raise ValueError(f"演出结束时间不晚于开始时间：{performance['id']}")

    if len({person["name"] for person in people}) != len(people):
        raise ValueError("人员数据中存在重名，无法唯一识别排班对象")
    if sum(person.get("gender") == "男" for person in people) == 0:
        raise ValueError("人员数据中没有男性，无法满足男性覆盖约束")

    artists = {performance["artist"] for performance in performances}
    for person in people:
        for category in RATING_CAPS:
            for artist in person.get(category, []):
                if artist not in artists:
                    warnings.append(f"{person['name']} 的偏好艺人未匹配到演出表：{artist}")
        for note in person.get("notes", []):
            warnings.append(f"{person['name']} 的自由备注未纳入约束：{note}")

    dates = sorted(ids, key=lambda value: tuple(int(part) for part in value.split("-")))
    return people, performances, warnings


def add_optional_shift(
    model: cp_model.CpModel,
    person_index: int,
    date_index: int,
    slot_index: int,
) -> dict[str, Any]:
    prefix = f"p{person_index}_d{date_index}_s{slot_index}"
    present = model.NewBoolVar(f"{prefix}_present")
    start = model.NewIntVar(0, WINDOW_MINUTES, f"{prefix}_start")
    end = model.NewIntVar(0, WINDOW_MINUTES, f"{prefix}_end")
    duration = model.NewIntVar(0, MAX_SHIFT, f"{prefix}_duration")
    interval = model.NewOptionalIntervalVar(start, duration, end, present, f"{prefix}_interval")
    model.Add(duration >= MIN_SHIFT).OnlyEnforceIf(present)
    model.Add(duration <= MAX_SHIFT).OnlyEnforceIf(present)
    model.Add(duration == 0).OnlyEnforceIf(present.Not())
    model.Add(start == 0).OnlyEnforceIf(present.Not())
    model.Add(end == 0).OnlyEnforceIf(present.Not())
    return {"present": present, "start": start, "end": end, "duration": duration, "interval": interval}


def build_model(people: list[dict[str, Any]], performances: list[dict[str, Any]], dates: list[str]):
    model = cp_model.CpModel()
    shifts: dict[tuple[int, int, int], dict[str, Any]] = {}
    by_person_date: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)

    # A person cannot have 8 stints in one 570-minute day: 8 x 40 minutes
    # on duty plus 7 x 40 minutes of rest would require 600 minutes.
    max_shifts_per_day = 7
    for p_index, person in enumerate(people):
        for d_index, _date in enumerate(dates):
            slots = []
            for slot_index in range(max_shifts_per_day):
                shift = add_optional_shift(model, p_index, d_index, slot_index)
                shifts[p_index, d_index, slot_index] = shift
                slots.append(shift)
                if slot_index:
                    previous = slots[slot_index - 1]
                    model.Add(shift["present"] <= previous["present"])
                    model.Add(shift["start"] >= previous["end"] + MIN_BREAK).OnlyEnforceIf(shift["present"])
            by_person_date[p_index, d_index] = slots

    for d_index, _date in enumerate(dates):
        all_duty_intervals = []
        all_off_intervals = []
        male_off_intervals = []
        for p_index, person in enumerate(people):
            slots = by_person_date[p_index, d_index]
            person_off = []
            first = slots[0]
            person_off.append(model.NewOptionalIntervalVar(
                0, first["start"], first["start"], first["present"], f"off_leading_p{p_index}_d{d_index}"
            ))
            no_shifts = model.NewBoolVar(f"no_shifts_p{p_index}_d{d_index}")
            model.Add(no_shifts + first["present"] == 1)
            person_off.append(model.NewOptionalIntervalVar(
                0, WINDOW_MINUTES, WINDOW_MINUTES, no_shifts, f"off_all_day_p{p_index}_d{d_index}"
            ))
            for slot_index, shift in enumerate(slots):
                all_duty_intervals.append(shift["interval"])
                if slot_index < len(slots) - 1:
                    next_shift = slots[slot_index + 1]
                    gap = model.NewIntVar(0, WINDOW_MINUTES, f"off_gap_p{p_index}_d{d_index}_s{slot_index}")
                    model.Add(gap == next_shift["start"] - shift["end"]).OnlyEnforceIf(next_shift["present"])
                    model.Add(gap == 0).OnlyEnforceIf(next_shift["present"].Not())
                    person_off.append(model.NewOptionalIntervalVar(
                        shift["end"], gap, next_shift["start"],
                        next_shift["present"], f"off_between_p{p_index}_d{d_index}_s{slot_index}"
                    ))
                    is_last = model.NewBoolVar(f"last_p{p_index}_d{d_index}_s{slot_index}")
                    model.Add(is_last <= shift["present"])
                    model.Add(is_last + next_shift["present"] <= 1)
                    model.Add(is_last >= shift["present"] - next_shift["present"])
                else:
                    is_last = shift["present"]
                trailing = model.NewIntVar(0, WINDOW_MINUTES, f"off_trailing_len_p{p_index}_d{d_index}_s{slot_index}")
                model.Add(trailing + shift["end"] == WINDOW_MINUTES).OnlyEnforceIf(is_last)
                model.Add(trailing == 0).OnlyEnforceIf(is_last.Not())
                person_off.append(model.NewOptionalIntervalVar(
                    shift["end"], trailing, WINDOW_MINUTES, is_last,
                    f"off_trailing_p{p_index}_d{d_index}_s{slot_index}"
                ))
            all_off_intervals.extend(person_off)
            if person.get("gender") == "男":
                male_off_intervals.extend(person_off)
        # Complement intervals make lower staffing a cumulative upper bound:
        # at most four people may be off duty, and at most all-but-one man.
        model.AddCumulative(all_off_intervals, [1] * len(all_off_intervals), len(people) - 4)
        male_count = sum(person.get("gender") == "男" for person in people)
        model.AddCumulative(male_off_intervals, [1] * len(male_off_intervals), male_count - 1)
        # Zero overlap is allowed by the <=3-minute handoff rule and keeps
        # coverage exactly at four people for this MVP.
        model.AddCumulative(all_duty_intervals, [1] * len(all_duty_intervals), 4)

    # Aggregate hours and shift-length preferences.
    hours: dict[int, cp_model.IntVar] = {}
    short_penalties: list[cp_model.IntVar] = []
    duration_deviations: list[cp_model.IntVar] = []
    shift_count_terms: list[cp_model.IntVar] = []
    closing_terms: dict[int, list[cp_model.IntVar]] = defaultdict(list)
    for p_index, _person in enumerate(people):
        person_durations = [
            shift["duration"]
            for d_index in range(len(dates))
            for shift in by_person_date[p_index, d_index]
        ]
        hours[p_index] = model.NewIntVar(0, len(dates) * WINDOW_MINUTES, f"hours_p{p_index}")
        model.Add(hours[p_index] == sum(person_durations))
        for d_index in range(len(dates)):
            for slot_index, shift in enumerate(by_person_date[p_index, d_index]):
                shift_count_terms.append(shift["present"])
                short = model.NewIntVar(0, 20, f"short_p{p_index}_d{d_index}_s{slot_index}")
                deviation = model.NewIntVar(0, MAX_SHIFT, f"duration_deviation_p{p_index}_d{d_index}_s{slot_index}")
                model.Add(short >= 60 - shift["duration"]).OnlyEnforceIf(shift["present"])
                model.Add(short == 0).OnlyEnforceIf(shift["present"].Not())
                model.AddAbsEquality(deviation, shift["duration"] - 75).OnlyEnforceIf(shift["present"])
                model.Add(deviation == 0).OnlyEnforceIf(shift["present"].Not())
                short_penalties.append(short)
                duration_deviations.append(deviation)
                closes = model.NewBoolVar(f"close_p{p_index}_d{d_index}_s{slot_index}")
                model.Add(shift["end"] == WINDOW_MINUTES).OnlyEnforceIf(closes)
                model.Add(shift["end"] != WINDOW_MINUTES).OnlyEnforceIf(closes.Not())
                model.Add(closes <= shift["present"])
                closing_terms[p_index].append(closes)

    max_hours = model.NewIntVar(0, len(dates) * WINDOW_MINUTES, "max_total_hours")
    min_hours = model.NewIntVar(0, len(dates) * WINDOW_MINUTES, "min_total_hours")
    model.AddMaxEquality(max_hours, list(hours.values()))
    model.AddMinEquality(min_hours, list(hours.values()))
    hours_range = model.NewIntVar(0, len(dates) * WINDOW_MINUTES, "total_hours_range")
    model.Add(hours_range == max_hours - min_hours)
    model.Add(hours_range <= MAX_HOURS_SPREAD)

    # A performance counts as viewable iff the person is off duty throughout
    # the performance plus the required five-minute buffers. Overlapping
    # performances are evaluated independently by design: if the person is
    # free for both, both preferences count as satisfied.
    scores: dict[int, cp_model.IntVar] = {}
    denominators: dict[int, int] = {}
    watched_by_person_category: dict[tuple[int, str], list[cp_model.IntVar]] = {}
    for p_index, person in enumerate(people):
        category_scores = []
        denominator = 0
        for category, cap in RATING_CAPS.items():
            artist_list = person.get(category, [])
            denominator += min(len(artist_list), cap) * RATING_POINTS[category]
            watched: list[cp_model.IntVar] = []
            for performance in performances:
                if performance["artist"] not in artist_list:
                    continue
                date_index = dates.index(performance["date"])
                guard_start = max(0, performance["start_minute"] - 5 - minute_of_day(WINDOW_START))
                guard_end = min(WINDOW_MINUTES, performance["end_minute"] + 5 - minute_of_day(WINDOW_START))
                if guard_end <= 0 or guard_start >= WINDOW_MINUTES:
                    continue
                guard_start = max(0, guard_start)
                guard_end = min(WINDOW_MINUTES, guard_end)
                watch = model.NewBoolVar(f"watch_p{p_index}_{performance['id']}")
                conflicts = []
                for slot_index, shift in enumerate(by_person_date[p_index, date_index]):
                    before = model.NewBoolVar(f"before_{p_index}_{performance['id']}_{slot_index}")
                    after = model.NewBoolVar(f"after_{p_index}_{performance['id']}_{slot_index}")
                    model.Add(shift["end"] <= guard_start).OnlyEnforceIf(before)
                    model.Add(shift["end"] >= guard_start + 1).OnlyEnforceIf(before.Not())
                    model.Add(shift["start"] >= guard_end).OnlyEnforceIf(after)
                    model.Add(shift["start"] <= guard_end - 1).OnlyEnforceIf(after.Not())
                    model.AddBoolOr([watch.Not(), before, after])
                    conflict = model.NewBoolVar(f"conflict_{p_index}_{performance['id']}_{slot_index}")
                    model.Add(conflict + before + after == 1)
                    conflicts.append(conflict)
                model.Add(watch + sum(conflicts) >= 1)
                watched.append(watch)
            watched_by_person_category[p_index, category] = watched
            capped = model.NewIntVar(0, cap, f"count_{category}_p{p_index}")
            model.AddMinEquality(capped, [sum(watched), cap])
            category_scores.append(capped * RATING_POINTS[category])
        scores[p_index] = model.NewIntVar(0, max(1, denominator), f"score_p{p_index}")
        model.Add(scores[p_index] == sum(category_scores))
        denominators[p_index] = denominator

    close_counts = {}
    for p_index in range(len(people)):
        close_counts[p_index] = model.NewIntVar(0, len(dates) * max_shifts_per_day, f"close_count_p{p_index}")
        model.Add(close_counts[p_index] == sum(closing_terms[p_index]))
    max_closes = model.NewIntVar(0, len(dates) * max_shifts_per_day, "max_closes")
    min_closes = model.NewIntVar(0, len(dates) * max_shifts_per_day, "min_closes")
    model.AddMaxEquality(max_closes, list(close_counts.values()))
    model.AddMinEquality(min_closes, list(close_counts.values()))
    close_range = model.NewIntVar(0, len(dates) * max_shifts_per_day, "close_range")
    model.Add(close_range == max_closes - min_closes)

    # Within must-see requests, a smaller submitted list has priority.
    # Separate stages make that priority strict instead of relying on weights.
    must_counts = sorted({len(person.get("must_see", [])) for person in people if person.get("must_see")})
    objectives = [
        (
            f"必须看（填报 {count} 场）",
            sum(
                sum(watched_by_person_category[p_index, "must_see"])
                for p_index, person in enumerate(people)
                if len(person.get("must_see", [])) == count
            ),
            "max",
        )
        for count in must_counts
    ] + [
        ("特别想看满足数", sum(w for p in range(len(people)) for w in watched_by_person_category[p, "very_want_to_see"]), "max"),
        ("有点想看满足数", sum(w for p in range(len(people)) for w in watched_by_person_category[p, "somewhat_want_to_see"]), "max"),
        ("工时差距", hours_range, "min"),
        ("短班次惩罚", sum(short_penalties), "min"),
        ("班次接近 75 分钟", sum(duration_deviations), "min"),
        ("收尾班差距", close_range, "min"),
        ("换班次数", sum(shift_count_terms), "min"),
    ]
    return model, shifts, by_person_date, {}, hours, scores, denominators, objectives


def validate_roster(roster: list[dict[str, Any]], people: list[dict[str, Any]], dates: list[str]) -> list[str]:
    """Independently check the exported intervals against hard rules."""
    errors: list[str] = []
    genders = {person["name"]: person.get("gender") for person in people}
    by_name = {person["name"]: person for person in roster}
    for person in roster:
        for shift in person["shifts"]:
            start = minute_of_day(shift["startTime"]) - minute_of_day(WINDOW_START)
            end = minute_of_day(shift["endTime"]) - minute_of_day(WINDOW_START)
            duration = end - start
            if start < 0 or end > WINDOW_MINUTES:
                errors.append(f"{person['name']} 的班次超出值班窗口")
            if not MIN_SHIFT <= duration <= MAX_SHIFT:
                errors.append(f"{person['name']} 的班次时长 {duration} 分钟不在 40–120 分钟内")
            if shift["durationMinutes"] != duration:
                errors.append(f"{person['name']} 的班次时长字段与起止时间不一致")

    for date in dates:
        day_shifts = {
            name: sorted((shift for shift in person["shifts"] if shift["date"] == date), key=lambda item: item["startTime"])
            for name, person in by_name.items()
        }
        for name, shifts in day_shifts.items():
            for first, second in zip(shifts, shifts[1:]):
                gap = minute_of_day(second["startTime"]) - minute_of_day(first["endTime"])
                if gap < MIN_BREAK:
                    errors.append(f"{name} 在 {date} 的相邻班次只间隔 {gap} 分钟")
        for minute in range(minute_of_day(WINDOW_START), minute_of_day(WINDOW_END)):
            on_duty = [
                name
                for name, shifts in day_shifts.items()
                if any(minute_of_day(shift["startTime"]) <= minute < minute_of_day(shift["endTime"]) for shift in shifts)
            ]
            if len(on_duty) < 4:
                errors.append(f"{date} {clock_time(minute)} 只有 {len(on_duty)} 人在岗")
                break
            if not any(genders[name] == "男" for name in on_duty):
                errors.append(f"{date} {clock_time(minute)} 没有男性在岗")
                break
        overstaffed = []
        for minute in range(minute_of_day(WINDOW_START), minute_of_day(WINDOW_END)):
            count = sum(
                any(minute_of_day(shift["startTime"]) <= minute < minute_of_day(shift["endTime"]) for shift in shifts)
                for shifts in day_shifts.values()
            )
            overstaffed.append(count > 4)
        if any(all(overstaffed[start : start + 4]) for start in range(len(overstaffed) - 3)):
            errors.append(f"{date} 存在超过 3 分钟的连续超额覆盖")
    return errors


def add_warm_start(model: cp_model.CpModel, shifts: dict, people: list[dict[str, Any]], dates: list[str], path: Path) -> bool:
    if not path.exists():
        return False
    try:
        roster = json.loads(path.read_text(encoding="utf-8"))["roster"]
        if validate_roster(roster, people, dates):
            return False
        by_name = {person["name"]: person for person in roster}
        if set(by_name) != {person["name"] for person in people}:
            return False
        date_index = {date: index for index, date in enumerate(dates)}
        person_index = {person["name"]: index for index, person in enumerate(people)}
        selected: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for name, person in by_name.items():
            for shift in person["shifts"]:
                if shift["date"] not in date_index:
                    return False
                selected[person_index[name], date_index[shift["date"]]].append(shift)
        for key, candidates in selected.items():
            candidates.sort(key=lambda item: item["startTime"])
            if len(candidates) > 7:
                return False
            p_index, d_index = key
            for slot_index, shift in enumerate(candidates):
                start = minute_of_day(shift["startTime"]) - minute_of_day(WINDOW_START)
                end = minute_of_day(shift["endTime"]) - minute_of_day(WINDOW_START)
                slot = shifts[p_index, d_index, slot_index]
                model.AddHint(slot["present"], 1)
                model.AddHint(slot["start"], start)
                model.AddHint(slot["end"], end)
                model.AddHint(slot["duration"], end - start)
        for p_index in range(len(people)):
            for d_index in range(len(dates)):
                count = len(selected[p_index, d_index])
                for slot_index in range(count, 7):
                    slot = shifts[p_index, d_index, slot_index]
                    model.AddHint(slot["present"], 0)
                    model.AddHint(slot["start"], 0)
                    model.AddHint(slot["end"], 0)
                    model.AddHint(slot["duration"], 0)
        return True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def write_report(result: dict[str, Any], dates: list[str], path: Path, output_name: str) -> None:
    roster = result["roster"]
    summary = result["requestSummary"]
    hours = [person["totalDutyMinutes"] for person in roster]

    def fraction(done: int, total: int) -> str:
        return f"{done}/{total}（{done / total:.2%}）" if total else "0/0（未填报）"

    lines = [
        "# 音乐节值班排班报告 v3",
        "",
        f"排班窗口：{WINDOW_START}–{WINDOW_END}，日期：{'、'.join(dates)}。",
        "",
        "## 排班摘要",
        "",
        f"- 全部需求满足：{fraction(summary['totalFulfilled'], summary['totalRequested'])}。",
        f"- 必须看满足：{fraction(summary['fulfilled']['must_see'], summary['requested']['must_see'])}。",
        f"- 特别想看满足：{fraction(summary['fulfilled']['very_want_to_see'], summary['requested']['very_want_to_see'])}。",
        f"- 有点想看满足：{fraction(summary['fulfilled']['somewhat_want_to_see'], summary['requested']['somewhat_want_to_see'])}。",
        f"- 三天个人总工时：{min(hours)}–{max(hours)} 分钟，差距 {max(hours) - min(hours)} 分钟。",
        "- 每天恰好 4 人在岗，至少 1 名男性在岗；交接重叠 0 分钟。",
        "",
    ]
    if V2_OUTPUT_PATH.exists() and path.name == REPORT_PATH.name:
        baseline = json.loads(V2_OUTPUT_PATH.read_text(encoding="utf-8"))
        old = baseline["requestSummary"]
        old_hours = [person["totalDutyMinutes"] for person in baseline["roster"]]
        lines.extend([
            "## 与 v2 对比",
            "",
            "| 指标 | v2 | v3 |",
            "| --- | ---: | ---: |",
            f"| 必须看 | {old['fulfilled']['must_see']}/{old['requested']['must_see']} | {summary['fulfilled']['must_see']}/{summary['requested']['must_see']} |",
            f"| 特别想看 | {old['fulfilled']['very_want_to_see']}/{old['requested']['very_want_to_see']} | {summary['fulfilled']['very_want_to_see']}/{summary['requested']['very_want_to_see']} |",
            f"| 有点想看 | {old['fulfilled']['somewhat_want_to_see']}/{old['requested']['somewhat_want_to_see']} | {summary['fulfilled']['somewhat_want_to_see']}/{summary['requested']['somewhat_want_to_see']} |",
            f"| 全部需求 | {old['totalFulfilled']}/{old['totalRequested']} | {summary['totalFulfilled']}/{summary['totalRequested']} |",
            f"| 三天工时差 | {max(old_hours) - min(old_hours)} 分钟 | {max(hours) - min(hours)} 分钟 |",
            "",
            "放宽工时差上限后，本轮搜索未找到满足更多观演需求的班表；`FEASIBLE` 阶段仍可能存在更优解。",
            "",
        ])
    lines.extend([
        "## 人员汇总",
        "",
        "| 人员 | 必须看 | 全部需求 | 限额偏好得分率 | 总工时 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for person in roster:
        must_done = person["fulfilledRequestCounts"]["must_see"]
        must_total = person["requestCounts"]["must_see"]
        done = sum(person["fulfilledRequestCounts"].values())
        total = sum(person["requestCounts"].values())
        capped = person["cappedPreferenceScoreRate"]
        lines.append(
            f"| {person['name']} | {must_done}/{must_total} | {done}/{total} | "
            f"{capped:.2%} | {person['totalDutyMinutes']} 分钟 |"
        )
    lines.extend(["", "## 未满足的观演需求", "", "| 人员 | 未满足需求 |", "| --- | --- |"])
    for person in roster:
        missed = person["unmetPreferences"]
        items = "、".join(f"{item['categoryName']}：{item['artist']}" for item in missed) or "无"
        lines.append(f"| {person['name']} | {items} |")
    lines.append("")
    lines.append("未满足表示当前班次与完整观演所需的演出时间及前后 5 分钟缓冲冲突；不代表这些需求在所有可行班表中都无法满足。")
    lines.append("同一时间的多场演出分别判断；只要都不与本人班次冲突，就都计为满足。")
    lines.extend(["", "## 每日班次", ""])
    for date in dates:
        lines.extend([f"### {date}", "", "| 人员 | 班次 | 当日值班分钟 |", "| --- | --- | ---: |"])
        for person in roster:
            day_shifts = [shift for shift in person["shifts"] if shift["date"] == date]
            formatted = "、".join(
                f"{shift['startTime']}–{shift['endTime']}（{shift['durationMinutes']} 分）"
                for shift in day_shifts
            ) or "无"
            lines.append(f"| {person['name']} | {formatted} | {sum(shift['durationMinutes'] for shift in day_shifts)} |")
        lines.append("")
    lines.extend(["## 优化阶段", "", "| 目标 | 状态 | 结果 |", "| --- | --- | ---: |"])
    for stage in result["solver"]["stages"]:
        value = stage["value"]
        lines.append(f"| {stage['objective']} | {stage['status']} | {value:.0f} |" if value is not None else f"| {stage['objective']} | {stage['status']} | — |")
    lines.extend(["", "`FEASIBLE` 表示找到可行解，但尚未证明该阶段达到全局最优。硬约束核对结果：通过。", ""])
    if result["warnings"]:
        lines.extend(["## 数据提示", ""])
        lines.extend(f"- {warning}" for warning in result["warnings"])
        lines.append("")
    lines.extend([
        "“可完整观看”表示排班允许观看，不代表实际观看记录。限额偏好得分率只计算每档最多 3/4/2 项，不代表全部需求满足率。",
        "",
        f"机器可读排班数据：[`{output_name}`]({output_name})。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def solve(people_path: Path, lineup_path: Path, output_path: Path, report_path: Path, time_limit: float) -> int:
    people, performances, warnings = read_inputs(people_path, lineup_path)
    dates = sorted(
        {performance["date"] for performance in performances},
        key=lambda value: tuple(int(part) for part in value.split("-")),
    )
    model, shifts, by_person_date, _active, hours, scores, denominators, objectives = build_model(people, performances, dates)
    seed_path = output_path if output_path.exists() else V2_OUTPUT_PATH
    seeded = add_warm_start(model, shifts, people, dates, seed_path)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8

    stage_results = []
    last_status = cp_model.UNKNOWN
    best_solution: dict[str, Any] | None = None
    if seeded:
        print("使用已有且通过硬规则核对的班表作为求解初始方案。")
    for label, objective, direction in objectives:
        model.Minimize(objective) if direction == "min" else model.Maximize(objective)
        status = solver.Solve(model)
        last_status = status
        stage_results.append({
            "objective": label,
            "status": solver.StatusName(status),
            "value": solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
        })
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        best_solution = {
            "shifts": {
                key: {
                    "present": solver.BooleanValue(shift["present"]),
                    "start": solver.Value(shift["start"]),
                    "end": solver.Value(shift["end"]),
                    "duration": solver.Value(shift["duration"]),
                }
                for key, shift in shifts.items()
            },
            "hours": {key: solver.Value(value) for key, value in hours.items()},
            "scores": {key: solver.Value(value) for key, value in scores.items()},
        }
        best = int(round(solver.ObjectiveValue()))
        if direction == "min":
            model.Add(objective <= best)
        else:
            model.Add(objective >= best)
        model.ClearHints()
        for key, shift in shifts.items():
            saved = best_solution["shifts"][key]
            model.AddHint(shift["present"], int(saved["present"]))
            model.AddHint(shift["start"], saved["start"])
            model.AddHint(shift["end"], saved["end"])
            model.AddHint(shift["duration"], saved["duration"])

    if best_solution is None:
        print("没有找到满足硬约束的班表。", file=sys.stderr)
        print(json.dumps(stage_results, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    roster = []
    for p_index, person in enumerate(people):
        person_shifts = []
        for d_index, date in enumerate(dates):
            for slot_index, shift in enumerate(by_person_date[p_index, d_index]):
                saved = best_solution["shifts"][p_index, d_index, slot_index]
                if not saved["present"]:
                    continue
                start = saved["start"]
                end = saved["end"]
                person_shifts.append({
                    "date": date,
                    "startTime": clock_time(minute_of_day(WINDOW_START) + start),
                    "endTime": clock_time(minute_of_day(WINDOW_START) + end),
                    "durationMinutes": end - start,
                })
        score = best_solution["scores"][p_index]
        denominator = denominators[p_index]
        fully_viewable = []
        for performance in performances:
            if performance["date"] not in dates:
                continue
            guard_start = performance["start_minute"] - 5
            guard_end = performance["end_minute"] + 5
            duty_conflict = any(
                shift["date"] == performance["date"]
                and minute_of_day(shift["startTime"]) < guard_end
                and guard_start < minute_of_day(shift["endTime"])
                for shift in person_shifts
            )
            if not duty_conflict:
                fully_viewable.append({
                    "performanceId": performance["id"],
                    "artist": performance["artist"],
                    "stageName": performance["stageName"],
                    "date": performance["date"],
                    "startTime": performance["startTime"],
                    "endTime": performance["endTime"],
                })
        viewable_artists = {performance["artist"] for performance in fully_viewable}
        unmet_preferences = [
            {
                "category": category,
                "categoryName": PREFERENCE_LABELS[category],
                "artist": artist,
                "reason": "与本人班次冲突",
            }
            for category in PREFERENCE_LABELS
            for artist in person.get(category, [])
            if artist not in viewable_artists
        ]
        request_counts = {category: len(person.get(category, [])) for category in PREFERENCE_LABELS}
        fulfilled_counts = {
            category: sum(artist in viewable_artists for artist in person.get(category, []))
            for category in PREFERENCE_LABELS
        }
        request_total = sum(request_counts.values())
        fulfilled_total = sum(fulfilled_counts.values())
        roster.append({
            "name": person["name"],
            "shifts": person_shifts,
            "fullyViewablePerformances": fully_viewable,
            "unmetPreferences": unmet_preferences,
            "totalDutyMinutes": best_solution["hours"][p_index],
            "preferenceScore": score,
            "maxPreferenceScore": denominator,
            "cappedPreferenceScoreRate": round(score / denominator, 4) if denominator else None,
            "requestCounts": request_counts,
            "fulfilledRequestCounts": fulfilled_counts,
            "requestFulfillmentRate": round(fulfilled_total / request_total, 4) if request_total else None,
        })

    validation_errors = validate_roster(roster, people, dates)
    if validation_errors:
        print("导出的班表未通过硬约束核对：", file=sys.stderr)
        for error in validation_errors:
            print(f"- {error}", file=sys.stderr)
        return 3

    request_counts = {
        category: sum(person["requestCounts"][category] for person in roster)
        for category in PREFERENCE_LABELS
    }
    fulfilled_counts = {
        category: sum(person["fulfilledRequestCounts"][category] for person in roster)
        for category in PREFERENCE_LABELS
    }
    request_total = sum(request_counts.values())
    fulfilled_total = sum(fulfilled_counts.values())
    result = {
        "optimizationVersion": "v3",
        "serviceWindow": {"startTime": WINDOW_START, "endTime": WINDOW_END},
        "rules": {
            "minimumStaff": 4,
            "minimumMen": 1,
            "minimumShiftMinutes": MIN_SHIFT,
            "maximumShiftMinutes": MAX_SHIFT,
            "minimumBreakBetweenShiftsMinutes": MIN_BREAK,
            "firstAndLastOffDutyPeriodsCount": False,
            "performanceBufferMinutes": 5,
            "handoffOverlapMaximumMinutes": 3,
            "actualOverstaffOverlapMinutes": 0,
            "maximumTotalDutySpreadMinutes": MAX_HOURS_SPREAD,
            "simultaneousPerformancesCountIndependently": True,
        },
        "requestSummary": {
            "requested": request_counts,
            "fulfilled": fulfilled_counts,
            "totalRequested": request_total,
            "totalFulfilled": fulfilled_total,
            "fulfillmentRate": round(fulfilled_total / request_total, 4) if request_total else None,
        },
        "solver": {"name": "OR-Tools CP-SAT", "stages": stage_results},
        "validation": {"hardConstraintsPassed": True},
        "warnings": warnings,
        "roster": roster,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(result, dates, report_path, output_path.name)
    print(f"已生成：{output_path}")
    print(f"求解状态：{solver.StatusName(last_status)}")
    for warning in warnings:
        print(f"数据提示：{warning}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成音乐节公平排班表")
    parser.add_argument("--people", type=Path, default=PEOPLE_PATH)
    parser.add_argument("--lineup", type=Path, default=LINEUP_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--time-limit", type=float, default=60.0, help="每个优化阶段的时间上限（秒）")
    args = parser.parse_args()
    return solve(args.people, args.lineup, args.output, args.report, args.time_limit)


if __name__ == "__main__":
    raise SystemExit(main())
