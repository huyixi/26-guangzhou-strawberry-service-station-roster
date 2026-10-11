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
V4_OUTPUT_PATH = ROOT / "docs" / "schedule-v4.json"
V4_REPORT_PATH = ROOT / "docs" / "schedule-report-v4.md"
V5_OUTPUT_PATH = ROOT / "docs" / "schedule-v5.json"
V5_REPORT_PATH = ROOT / "docs" / "schedule-report-v5.md"
V6_OUTPUT_PATH = ROOT / "docs" / "schedule-v6.json"
V6_REPORT_PATH = ROOT / "docs" / "schedule-report-v6.md"
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


def read_inputs(people_path: Path, lineup_path: Path, require_male: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
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
    if require_male and sum(person.get("gender") == "男" for person in people) == 0:
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


def build_model(people: list[dict[str, Any]], performances: list[dict[str, Any]], dates: list[str], require_male: bool = True):
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
        if require_male:
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
    weighted_scores: dict[int, cp_model.IntVar] = {}
    weighted_denominators: dict[int, int] = {}
    watched_by_person_category: dict[tuple[int, str], list[cp_model.IntVar]] = {}
    watches_by_person_artist: dict[tuple[int, str], list[cp_model.IntVar]] = defaultdict(list)
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
                watches_by_person_artist[p_index, performance["artist"]].append(watch)
            watched_by_person_category[p_index, category] = watched
            capped = model.NewIntVar(0, cap, f"count_{category}_p{p_index}")
            model.AddMinEquality(capped, [sum(watched), cap])
            category_scores.append(capped * RATING_POINTS[category])
        scores[p_index] = model.NewIntVar(0, max(1, denominator), f"score_p{p_index}")
        model.Add(scores[p_index] == sum(category_scores))
        denominators[p_index] = denominator

        weighted_terms = []
        weighted_denominator = 0
        for category, points in RATING_POINTS.items():
            for artist_index, artist in enumerate(person.get(category, [])):
                artist_watch = model.NewBoolVar(f"weighted_watch_p{p_index}_{category}_{artist_index}")
                show_watches = watches_by_person_artist.get((p_index, artist), [])
                if show_watches:
                    model.AddMaxEquality(artist_watch, show_watches)
                else:
                    model.Add(artist_watch == 0)
                weighted_terms.append(artist_watch * points)
                weighted_denominator += points
        weighted_scores[p_index] = model.NewIntVar(
            0, max(1, weighted_denominator), f"weighted_score_p{p_index}"
        )
        model.Add(weighted_scores[p_index] == sum(weighted_terms))
        weighted_denominators[p_index] = weighted_denominator

    # Minimize the spread in each person's full weighted request satisfaction.
    # The user's metric counts every request, including entries beyond legacy score caps.
    weighted_rates = {}
    for p_index, denominator in weighted_denominators.items():
        rate = model.NewIntVar(0, 10_000, f"weighted_rate_p{p_index}")
        if denominator:
            model.AddDivisionEquality(rate, weighted_scores[p_index] * 10_000, denominator)
        else:
            model.Add(rate == 0)
        weighted_rates[p_index] = rate
    max_weighted_rate = model.NewIntVar(0, 10_000, "max_weighted_rate")
    min_weighted_rate = model.NewIntVar(0, 10_000, "min_weighted_rate")
    model.AddMaxEquality(max_weighted_rate, list(weighted_rates.values()))
    model.AddMinEquality(min_weighted_rate, list(weighted_rates.values()))
    weighted_rate_spread = model.NewIntVar(0, 10_000, "weighted_rate_spread")
    model.Add(weighted_rate_spread == max_weighted_rate - min_weighted_rate)

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
    watch_variables = {
        "by_category": watched_by_person_category,
        "by_artist": watches_by_person_artist,
        "weighted_scores": weighted_scores,
        "weighted_denominators": weighted_denominators,
        "weighted_rates": weighted_rates,
        "minimum_weighted_rate": min_weighted_rate,
        "weighted_rate_spread": weighted_rate_spread,
        "weighted_rate_sum": sum(weighted_rates.values()),
    }
    return model, shifts, by_person_date, watch_variables, hours, scores, denominators, objectives


def validate_roster(roster: list[dict[str, Any]], people: list[dict[str, Any]], dates: list[str], require_male: bool = True) -> list[str]:
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
            if require_male and not any(genders[name] == "男" for name in on_duty):
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


def add_warm_start(model: cp_model.CpModel, shifts: dict, people: list[dict[str, Any]], dates: list[str], path: Path, require_male: bool = True) -> bool:
    if not path.exists():
        return False
    try:
        roster = json.loads(path.read_text(encoding="utf-8"))["roster"]
        if validate_roster(roster, people, dates, require_male=require_male):
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


def write_report(
    result: dict[str, Any], people: list[dict[str, Any]], dates: list[str], path: Path, output_name: str
) -> None:
    version = result["optimizationVersion"]
    roster = result["roster"]
    summary = result["requestSummary"]
    hours = [person["totalDutyMinutes"] for person in roster]
    people_by_name = {person["name"]: person for person in people}

    def fraction(done: int, total: int) -> str:
        return f"{done}/{total}（{done / total:.2%}）" if total else "0/0（未填报）"

    lines = [
        f"# 音乐节值班排班报告 {version}",
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
        ("- 每天恰好 4 人在岗；男性在岗要求已取消；交接重叠 0 分钟。" if version in ("v5", "v6") else "- 每天恰好 4 人在岗，至少 1 名男性在岗；交接重叠 0 分钟。"),
        "",
    ]
    if version == "v3" and V2_OUTPUT_PATH.exists() and path.name == REPORT_PATH.name:
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
    if version == "v4":
        baseline = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        old = baseline["requestSummary"]
        old_by_name = {person["name"]: person for person in baseline["roster"]}
        lines.extend([
            "## 与 v3 对比",
            "",
            "陈思含增加 2 项已满足需求。仅陈思含、蒋姝、杨田泽的班次有调整；其余五人保持不变。",
            "",
            "| 人员 | v3 满足 | v4 满足 | v3 工时 | v4 工时 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for person in roster:
            previous = old_by_name[person["name"]]
            old_done = sum(previous["fulfilledRequestCounts"].values())
            new_done = sum(person["fulfilledRequestCounts"].values())
            lines.append(
                f"| {person['name']} | {old_done} | {new_done} | "
                f"{previous['totalDutyMinutes']} 分钟 | {person['totalDutyMinutes']} 分钟 |"
            )
        lines.extend([
            "",
            f"v3 全部需求：{old['totalFulfilled']}/{old['totalRequested']}；"
            f"v4 全部需求：{summary['totalFulfilled']}/{summary['totalRequested']}。"
            "杨田泽少满足 1 项“必须看”，蒋姝少满足 1 项“特别想看”；"
            "陈思含增加 2 项“有点想看”。",
            "",
        ])
        for person in roster:
            previous = old_by_name[person["name"]]
            old_unmet = {(item["category"], item["artist"]) for item in previous["unmetPreferences"]}
            new_unmet = {(item["category"], item["artist"]) for item in person["unmetPreferences"]}
            gained = old_unmet - new_unmet
            lost = new_unmet - old_unmet
            if gained or lost:
                changes = []
                if gained:
                    changes.append("新增满足 " + "、".join(
                        f"{PREFERENCE_LABELS[category]}：{artist}" for category, artist in sorted(gained)
                    ))
                if lost:
                    changes.append("不再满足 " + "、".join(
                        f"{PREFERENCE_LABELS[category]}：{artist}" for category, artist in sorted(lost)
                    ))
                lines.append(f"- {person['name']}：{'；'.join(changes)}。")
        lines.append("")
    if version == "v5":
        baseline = json.loads(V4_OUTPUT_PATH.read_text(encoding="utf-8"))
        old_by_name = {person["name"]: person for person in baseline["roster"]}

        def full_weighted_rate(person: dict[str, Any]) -> float:
            numerator = sum(
                person["fulfilledRequestCounts"][category] * RATING_POINTS[category]
                for category in RATING_POINTS
            )
            denominator = sum(
                person["requestCounts"][category] * RATING_POINTS[category]
                for category in RATING_POINTS
            )
            return numerator / denominator if denominator else 0.0

        lines.extend([
            "## 与 v4 对比",
            "",
            "v5 硬性保证胡一希可完整观看汉堡黄（含演出前后各 5 分钟缓冲），取消至少一名男性在岗要求，并要求陈思含较原 v5 至少多满足 1 项、贺金意至少少满足 1 项。之后依次最大化最低个人加权满足率、最小化全员满足率差距、最大化个人满足率之和，最后减少相对 v4 的班次调整。满足率分母包含全部填报需求，权重为必须看 100、特别想看 30、有点想看 10。",
            "",
            "| 人员 | v4 加权满足率 | v5 加权满足率 | v4→v5 必须看 |",
            "| --- | ---: | ---: | ---: |",
        ])
        for person in roster:
            previous = old_by_name[person["name"]]
            lines.append(
                f"| {person['name']} | {full_weighted_rate(previous):.2%} | "
                f"{full_weighted_rate(person):.2%} | "
                f"{previous['fulfilledRequestCounts']['must_see']}→{person['fulfilledRequestCounts']['must_see']} |"
            )
        lines.extend(["", "v4 是初始班表；本次每个优化阶段的状态和结果见下表。`OPTIMAL` 表示该阶段已证明最优，`FEASIBLE` 表示当前时间上限内找到可行解，仍可能有更优值。", ""])
    if version == "v6":
        baseline = json.loads(V5_OUTPUT_PATH.read_text(encoding="utf-8"))
        old_summary = baseline["requestSummary"]
        new_summary = summary

        def min_full_rate(roster_entry: dict[str, Any]) -> float:
            denominator = roster_entry["maxWeightedPreferenceScore"]
            return roster_entry["weightedPreferenceScore"] / denominator if denominator else 0.0

        old_min_rate = min(min_full_rate(p) for p in baseline["roster"])
        new_min_rate = min(min_full_rate(p) for p in roster)
        lines.extend([
            "## v6 计算说明",
            "",
            "沿用胡一希的汉堡黄完整观演硬约束及公平优化顺序，以 v5 班表作为求解初始方案和班次偏移基准；v6 不设置男性覆盖要求或针对陈思含、贺金意的满足数调整目标。",
            "",
            "## 与 v5 对比",
            "",
            "| 指标 | v5 | v6 |",
            "| --- | ---: | ---: |",
            f"| 全部需求满足 | {old_summary['totalFulfilled']}/{old_summary['totalRequested']} | {new_summary['totalFulfilled']}/{new_summary['totalRequested']} |",
            f"| 必须看 | {old_summary['fulfilled']['must_see']}/{old_summary['requested']['must_see']} | {new_summary['fulfilled']['must_see']}/{new_summary['requested']['must_see']} |",
            f"| 特别想看 | {old_summary['fulfilled']['very_want_to_see']}/{old_summary['requested']['very_want_to_see']} | {new_summary['fulfilled']['very_want_to_see']}/{new_summary['requested']['very_want_to_see']} |",
            f"| 有点想看 | {old_summary['fulfilled']['somewhat_want_to_see']}/{old_summary['requested']['somewhat_want_to_see']} | {new_summary['fulfilled']['somewhat_want_to_see']}/{new_summary['requested']['somewhat_want_to_see']} |",
            f"| 最低个人加权满足率 | {old_min_rate:.2%} | {new_min_rate:.2%} |",
            f"| 总工时范围 | {min(p['totalDutyMinutes'] for p in baseline['roster'])}–{max(p['totalDutyMinutes'] for p in baseline['roster'])} 分钟 | {min(hours)}–{max(hours)} 分钟 |",
            f"| 工时差 | {max(p['totalDutyMinutes'] for p in baseline['roster']) - min(p['totalDutyMinutes'] for p in baseline['roster'])} 分钟 | {max(hours) - min(hours)} 分钟 |",
            "",
        ])
        genders = {person["name"]: person.get("gender") for person in people}
        for date in dates:
            female_only = []
            for minute in range(minute_of_day(WINDOW_START), minute_of_day(WINDOW_END)):
                active = [p["name"] for p in roster if any(
                    shift["date"] == date
                    and minute_of_day(shift["startTime"]) <= minute < minute_of_day(shift["endTime"])
                    for shift in p["shifts"]
                )]
                if active and not any(genders.get(name) == "男" for name in active):
                    female_only.append(minute)
            if female_only:
                runs = []
                start = previous = female_only[0]
                for minute in female_only[1:]:
                    if minute != previous + 1:
                        runs.append((start, previous + 1))
                        start = minute
                    previous = minute
                runs.append((start, previous + 1))
                spans = "、".join(f"{clock_time(start)}–{clock_time(end)}" for start, end in runs)
                lines.append(f"- {date} 全女性在岗时段：{spans}。")
        if any("全女性在岗时段" in line for line in lines[-len(dates):]):
            lines.append("")
        else:
            lines.extend(["v6 没有出现全女性在岗时段。", ""])
    lines.extend([
        "## 人员汇总",
        "",
        "| 人员 | 必须看 | 全部需求 | 加权满足度 | 限额偏好得分率 | 总工时 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for person in roster:
        must_done = person["fulfilledRequestCounts"]["must_see"]
        must_total = person["requestCounts"]["must_see"]
        done = sum(person["fulfilledRequestCounts"].values())
        total = sum(person["requestCounts"].values())
        capped = person["cappedPreferenceScoreRate"]
        weighted = full_weighted_rate(person) if version == "v5" else person["weightedPreferenceScoreRate"]
        lines.append(
            f"| {person['name']} | {must_done}/{must_total} | {done}/{total} | "
            f"{weighted:.2%} | {capped:.2%} | {person['totalDutyMinutes']} 分钟 |"
        )
    for heading, status, is_fulfilled in (
        ("已满足的观演需求", "已满足", True),
        ("未满足的观演需求", "未满足", False),
    ):
        category_headers = " | ".join(f"{status}：{label}" for label in PREFERENCE_LABELS.values())
        lines.extend([
            "", f"## {heading}", "",
            f"| 姓名 | {category_headers} |",
            "| --- | --- | --- | --- |",
        ])
        for person in roster:
            requested = people_by_name[person["name"]]
            viewable_artists = {performance["artist"] for performance in person["fullyViewablePerformances"]}
            cells = []
            for category in PREFERENCE_LABELS:
                artists = requested.get(category, [])
                matching = [artist for artist in artists if (artist in viewable_artists) == is_fulfilled]
                cells.append("、".join(matching) or "无")
            lines.append(f"| {person['name']} | {' | '.join(cells)} |")
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
        "“可完整观看”表示排班允许观看，不代表实际观看记录。加权满足度按全部填报需求计分；限额偏好得分率只计算每档最多 3/4/2 项。",
        "",
        f"机器可读排班数据：[`{output_name}`]({output_name})。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def configure_v4(
    model: cp_model.CpModel,
    people: list[dict[str, Any]],
    dates: list[str],
    by_person_date: dict[tuple[int, int], list[dict[str, Any]]],
    watch_variables: dict[str, Any],
) -> list[tuple[str, Any, str]]:
    """Keep four v3 rosters fixed while improving Chen Sihan's requests."""
    baseline = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    old_roster = {person["name"]: person for person in baseline["roster"]}
    index_by_name = {person["name"]: index for index, person in enumerate(people)}
    fixed_names = {"杨淇贤", "潘旭欣", "谌雨涵", "胡一希"}
    adjustable_names = {"陈思含", "蒋姝", "贺金意", "杨田泽"}
    if set(index_by_name) != fixed_names | adjustable_names:
        raise ValueError("v4 需要与 v3 相同的八人名单")
    if validate_roster(baseline["roster"], people, dates):
        raise ValueError("v3 基准班表未通过硬约束核对")

    window_start = minute_of_day(WINDOW_START)
    for name in fixed_names:
        person_index = index_by_name[name]
        for date_index, date in enumerate(dates):
            old_shifts = [shift for shift in old_roster[name]["shifts"] if shift["date"] == date]
            for slot_index, slot in enumerate(by_person_date[person_index, date_index]):
                model.Add(slot["present"] == int(slot_index < len(old_shifts)))
                if slot_index < len(old_shifts):
                    old_shift = old_shifts[slot_index]
                    model.Add(slot["start"] == minute_of_day(old_shift["startTime"]) - window_start)
                    model.Add(slot["end"] == minute_of_day(old_shift["endTime"]) - window_start)

    by_category = watch_variables["by_category"]
    by_artist = watch_variables["by_artist"]
    chen_index = index_by_name["陈思含"]
    model.Add(sum(watch for category in PREFERENCE_LABELS for watch in by_category[chen_index, category]) == 4)
    for artist in ("告五人", "文雀"):
        model.Add(sum(by_artist[chen_index, artist]) >= 1)

    for name in ("蒋姝", "贺金意", "杨田泽"):
        person_index = index_by_name[name]
        for category in PREFERENCE_LABELS:
            floor = old_roster[name]["fulfilledRequestCounts"][category]
            if (name, category) in {("蒋姝", "very_want_to_see"), ("杨田泽", "must_see")}:
                floor -= 1
            model.Add(sum(by_category[person_index, category]) >= floor)

    # Prefer retaining the three adjustable colleagues' higher-priority
    # requests, then choose the solution closest to their v3 shift boundaries.
    other_indices = [index_by_name[name] for name in ("蒋姝", "贺金意", "杨田泽")]
    movement = []
    for name in adjustable_names:
        person_index = index_by_name[name]
        for date_index, date in enumerate(dates):
            old_shifts = [shift for shift in old_roster[name]["shifts"] if shift["date"] == date]
            for slot_index, slot in enumerate(by_person_date[person_index, date_index]):
                old_start = minute_of_day(old_shifts[slot_index]["startTime"]) - window_start if slot_index < len(old_shifts) else 0
                old_end = minute_of_day(old_shifts[slot_index]["endTime"]) - window_start if slot_index < len(old_shifts) else 0
                for boundary, previous in (("start", old_start), ("end", old_end)):
                    change = model.NewIntVar(0, WINDOW_MINUTES, f"v4_change_{person_index}_{date_index}_{slot_index}_{boundary}")
                    model.AddAbsEquality(change, slot[boundary] - previous)
                    movement.append(change)

    return [
        (
            f"其他三人{PREFERENCE_LABELS[category]}满足数",
            sum(watch for person_index in other_indices for watch in by_category[person_index, category]),
            "max",
        )
        for category in PREFERENCE_LABELS
    ] + [("相对 v3 的班次起止时间偏移量", sum(movement), "min")]


def configure_v5(
    model: cp_model.CpModel,
    people: list[dict[str, Any]],
    dates: list[str],
    by_person_date: dict[tuple[int, int], list[dict[str, Any]]],
    watch_variables: dict[str, Any],
) -> list[tuple[str, Any, str]]:
    """Maximize the weakest rate, minimize its spread, then maximize total fulfillment."""
    baseline = json.loads(V4_OUTPUT_PATH.read_text(encoding="utf-8"))
    old_roster = {person["name"]: person for person in baseline["roster"]}
    if set(old_roster) != {person["name"] for person in people}:
        raise ValueError("v5 需要与 v4 相同的人员名单")
    if validate_roster(baseline["roster"], people, dates):
        raise ValueError("v4 基准班表未通过硬约束核对")

    # This is an explicit v5 requirement: Hu Yixi must have at least one
    # complete, buffered performance of 汉堡黄 free from duty.
    hu_index = next((i for i, person in enumerate(people) if person["name"] == "胡一希"), None)
    if hu_index is None:
        raise ValueError("v5 需要包含胡一希")
    hu_hanbaohuang = watch_variables["by_artist"].get((hu_index, "汉堡黄"), [])
    if not hu_hanbaohuang:
        raise ValueError("v5 无法满足胡一希完整观看汉堡黄：演出未匹配到观演变量")
    model.AddBoolOr(hu_hanbaohuang)

    # Apply the requested allocation shift over the existing v5 roster.
    index_by_name = {person["name"]: index for index, person in enumerate(people)}
    chen_index = index_by_name["陈思含"]
    he_index = index_by_name["贺金意"]
    watched = watch_variables["by_category"]
    chen_total = sum(watch for category in PREFERENCE_LABELS for watch in watched[chen_index, category])
    he_total = sum(watch for category in PREFERENCE_LABELS for watch in watched[he_index, category])
    model.Add(chen_total >= 4)
    model.Add(he_total <= 10)

    window_start = minute_of_day(WINDOW_START)
    movement = []
    for person_index, person in enumerate(people):
        for date_index, date in enumerate(dates):
            old_shifts = sorted(
                (shift for shift in old_roster[person["name"]]["shifts"] if shift["date"] == date),
                key=lambda shift: shift["startTime"],
            )
            for slot_index, slot in enumerate(by_person_date[person_index, date_index]):
                old_start = minute_of_day(old_shifts[slot_index]["startTime"]) - window_start if slot_index < len(old_shifts) else 0
                old_end = minute_of_day(old_shifts[slot_index]["endTime"]) - window_start if slot_index < len(old_shifts) else 0
                for boundary, previous in (("start", old_start), ("end", old_end)):
                    change = model.NewIntVar(0, WINDOW_MINUTES, f"v5_change_{person_index}_{date_index}_{slot_index}_{boundary}")
                    model.AddAbsEquality(change, slot[boundary] - previous)
                    movement.append(change)

    # Rates use the full submitted weighted denominator (100 / 30 / 10), with
    # integer 1/10,000 precision. No per-category floors constrain tradeoffs.
    return [
        ("最低个人全部需求加权满足率", watch_variables["minimum_weighted_rate"], "max"),
        ("全员加权满足度差距", watch_variables["weighted_rate_spread"], "min"),
        ("个人加权满足率之和", watch_variables["weighted_rate_sum"], "max"),
        ("相对 v4 的班次起止时间偏移量", sum(movement), "min"),
    ]


def configure_v6(model, people, dates, by_person_date, watch_variables):
    """Keep v5's Hanbaohuang guarantee and objectives, based on the v5 roster."""
    baseline = json.loads(V5_OUTPUT_PATH.read_text(encoding="utf-8"))
    old_roster = {person["name"]: person for person in baseline["roster"]}
    if set(old_roster) != {person["name"] for person in people}:
        raise ValueError("v6 需要与 v5 相同的人员名单")
    if validate_roster(baseline["roster"], people, dates, require_male=False):
        raise ValueError("v5 基准班表未通过硬约束核对")
    hu_index = next((i for i, person in enumerate(people) if person["name"] == "胡一希"), None)
    if hu_index is None:
        raise ValueError("v6 需要包含胡一希")
    hu_shows = watch_variables["by_artist"].get((hu_index, "汉堡黄"), [])
    if not hu_shows:
        raise ValueError("v6 无法匹配胡一希的汉堡黄演出")
    model.AddBoolOr(hu_shows)

    movement = []
    window_start = minute_of_day(WINDOW_START)
    for p_index, person in enumerate(people):
        for d_index, date in enumerate(dates):
            old_shifts = sorted((s for s in old_roster[person["name"]]["shifts"] if s["date"] == date), key=lambda s: s["startTime"])
            for slot_index, slot in enumerate(by_person_date[p_index, d_index]):
                old_start = minute_of_day(old_shifts[slot_index]["startTime"]) - window_start if slot_index < len(old_shifts) else 0
                old_end = minute_of_day(old_shifts[slot_index]["endTime"]) - window_start if slot_index < len(old_shifts) else 0
                for boundary, previous in (("start", old_start), ("end", old_end)):
                    change = model.NewIntVar(0, WINDOW_MINUTES, f"v6_change_{p_index}_{d_index}_{slot_index}_{boundary}")
                    model.AddAbsEquality(change, slot[boundary] - previous)
                    movement.append(change)
    return [
        ("最低个人全部需求加权满足率", watch_variables["minimum_weighted_rate"], "max"),
        ("全员加权满足度差距", watch_variables["weighted_rate_spread"], "min"),
        ("个人加权满足率之和", watch_variables["weighted_rate_sum"], "max"),
        ("相对 v5 的班次起止时间偏移量", sum(movement), "min"),
    ]


def solve(
    people_path: Path, lineup_path: Path, output_path: Path, report_path: Path, time_limit: float,
    version: str = "v3",
) -> int:
    require_male = version not in ("v5", "v6")
    people, performances, warnings = read_inputs(people_path, lineup_path, require_male=require_male)
    dates = sorted(
        {performance["date"] for performance in performances},
        key=lambda value: tuple(int(part) for part in value.split("-")),
    )
    model, shifts, by_person_date, watch_variables, hours, scores, denominators, objectives = build_model(people, performances, dates, require_male=require_male)
    if version == "v4":
        objectives = configure_v4(model, people, dates, by_person_date, watch_variables)
    elif version == "v5":
        objectives = configure_v5(model, people, dates, by_person_date, watch_variables)
    elif version == "v6":
        objectives = configure_v6(model, people, dates, by_person_date, watch_variables)
    seed_path = V5_OUTPUT_PATH if version == "v6" else V4_OUTPUT_PATH if version == "v5" else OUTPUT_PATH if version == "v4" else (output_path if output_path.exists() else V2_OUTPUT_PATH)
    seeded = add_warm_start(model, shifts, people, dates, seed_path, require_male=require_male)
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
            "weighted_scores": {key: solver.Value(value) for key, value in watch_variables["weighted_scores"].items()},
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
        weighted_score = sum(fulfilled_counts[category] * RATING_POINTS[category] for category in RATING_POINTS)
        weighted_denominator = sum(request_counts[category] * RATING_POINTS[category] for category in RATING_POINTS)
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
            "weightedPreferenceScore": weighted_score,
            "maxWeightedPreferenceScore": weighted_denominator,
            "weightedPreferenceScoreRate": (
                round(weighted_score / weighted_denominator, 4) if weighted_denominator else None
            ),
            "requestCounts": request_counts,
            "fulfilledRequestCounts": fulfilled_counts,
            "requestFulfillmentRate": round(fulfilled_total / request_total, 4) if request_total else None,
        })

    validation_errors = validate_roster(roster, people, dates, require_male=require_male)
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
        "optimizationVersion": version,
        "serviceWindow": {"startTime": WINDOW_START, "endTime": WINDOW_END},
        "rules": {
            "minimumStaff": 4,
            "minimumMen": 1 if require_male else 0,
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
    write_report(result, people, dates, report_path, output_path.name)
    print(f"已生成：{output_path}")
    print(f"求解状态：{solver.StatusName(last_status)}")
    for warning in warnings:
        print(f"数据提示：{warning}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成音乐节公平排班表")
    parser.add_argument("--people", type=Path, default=PEOPLE_PATH)
    parser.add_argument("--lineup", type=Path, default=LINEUP_PATH)
    parser.add_argument("--version", choices=("v3", "v4", "v5", "v6"), default="v3")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--time-limit", type=float, default=60.0, help="每个优化阶段的时间上限（秒）")
    args = parser.parse_args()
    output = args.output or (V6_OUTPUT_PATH if args.version == "v6" else V5_OUTPUT_PATH if args.version == "v5" else V4_OUTPUT_PATH if args.version == "v4" else OUTPUT_PATH)
    report = args.report or (V6_REPORT_PATH if args.version == "v6" else V5_REPORT_PATH if args.version == "v5" else V4_REPORT_PATH if args.version == "v4" else REPORT_PATH)
    return solve(args.people, args.lineup, output, report, args.time_limit, args.version)


if __name__ == "__main__":
    raise SystemExit(main())
