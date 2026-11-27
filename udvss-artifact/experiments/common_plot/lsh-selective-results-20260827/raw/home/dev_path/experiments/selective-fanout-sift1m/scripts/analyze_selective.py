#!/usr/bin/env python3
"""Offline stopping-rule comparison for selective PG fan-out.

Reads the per-(query, stage) CSV written by ceph-vector-bench-native's
--selective-csv and replays candidate stopping rules against it. Every query
in a --selective-stop never run carries a row for every stage, so a rule can
be simulated exactly without re-running the cluster.

  ./analyze_selective.py results/telemetry_m96.csv
"""
import collections
import csv
import sys


def load(path):
    rows = list(csv.DictReader(open(path)))
    queries = collections.defaultdict(dict)
    for row in rows:
        queries[int(row["query_index"])][int(row["stage"])] = row
    schedule = sorted({int(r["cum_m"]) for r in rows})
    return queries, schedule


def simulate(queries, stage_indices, should_stop):
    """Walk stage_indices in order, stopping when should_stop fires.

    should_stop(step, row) -> bool, where step counts dispatched stages.
    Returns (recall, avg_m, avg_stages, avg_candidates).
    """
    recall = m = stages = candidates = 0
    for stage_rows in queries.values():
        row = None
        for step, stage in enumerate(stage_indices):
            row = stage_rows[stage]
            if step == len(stage_indices) - 1 or should_stop(step, row):
                break
        recall += float(row["recall_at_stage"])
        m += int(row["cum_m"])
        stages += step + 1
        candidates += int(row["cum_candidates"])
    n = len(queries)
    return recall / n, m / n, (stages / n), candidates / n


def oracle(queries, schedule):
    """Smallest stage at which a query already holds its final recall.

    Upper bound on what any stopping rule could achieve: it stops as early as
    possible while losing nothing, but needs the answer to do it.
    """
    n = len(queries)
    m = stages = recall = 0
    stop_at = collections.Counter()
    for stage_rows in queries.values():
        final = float(stage_rows[len(schedule) - 1]["recall_at_stage"])
        stage = next(i for i in range(len(schedule))
                     if float(stage_rows[i]["recall_at_stage"]) >= final)
        m += schedule[stage]
        stages += stage + 1
        recall += final
        stop_at[schedule[stage]] += 1
    return recall / n, m / n, stages / n, stop_at


def tau_rule(threshold):
    def rule(_step, row):
        # An unset tau means top-k was never filled -- no bound exists yet,
        # which is not the same as "the bound stopped improving".
        if row["tau"] == "" or row["tau_impr_rel"] == "":
            return False
        return float(row["tau_impr_rel"]) < threshold
    return rule


def churn_rule(max_replacements):
    def rule(_step, row):
        if row["tau"] == "":
            return False
        return int(row["topk_replacements"]) <= max_replacements
    return rule


def main(path):
    queries, schedule = load(path)
    n = len(queries)
    last = len(schedule) - 1
    every = list(range(len(schedule)))
    full_recall = simulate(queries, every, lambda s, r: False)[0]

    def show(name, result):
        recall, m, stages, _ = result
        print(f"{name:<48} {recall:>10.5f} {m:>7.1f} {stages:>8.2f}"
              f" {100 * recall / full_recall:>7.1f}%")

    print(f"{len(queries)} queries, schedule {schedule}\n")
    print(f"{'rule':<48} {'recall@10':>10} {'avg_M':>7} {'avg_stg':>8} {'vs max':>8}")
    print("-" * 86)
    for i, m in enumerate(schedule):
        show(f"fixed M={m}", simulate(queries, every, lambda s, r, i=i: s >= i))
    recall, m, stages, stop_at = oracle(queries, schedule)
    print("-" * 86)
    show("ORACLE (needs the answer; upper bound)", (recall, m, stages, 0))
    print(f"{'':48}   oracle stops at: "
          + ", ".join(f"M={k}:{stop_at[k]}" for k in schedule))

    print("-" * 86)
    for threshold in (0.001, 0.01, 0.05):
        show(f"tau rel-improvement < {threshold}",
             simulate(queries, every, tau_rule(threshold)))
    for k in (0, 1, 2):
        show(f"topk_replacements <= {k}",
             simulate(queries, every, churn_rule(k)))

    # Why the rules above cap out: check whether the stage before a late gain
    # showed any local sign that more PGs were worth fetching.
    print("-" * 86)
    mid = last - 2
    late = [q for q, s in queries.items()
            if float(s[last]["recall_at_stage"]) > float(s[mid]["recall_at_stage"])]
    if late:
        blind = sum(1 for q in late
                    if int(queries[q][mid]["topk_replacements"]) == 0
                    and (queries[q][mid]["tau_impr_rel"] == ""
                         or float(queries[q][mid]["tau_impr_rel"]) == 0.0))
        print(f"queries gaining recall after M={schedule[mid]}: {len(late)}/{n}")
        print(f"  of those, showing NO local signal at M={schedule[mid]} "
              f"(zero top-k churn AND zero tau movement): "
              f"{blind} ({100 * blind / len(late):.1f}%)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/telemetry_m96.csv")
