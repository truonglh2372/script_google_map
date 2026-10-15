"""Offline self-test for the alt.py id selection rule (no network, stdlib only).

Run: python tools/test_id_filter.py
It extracts the real functions from windows/alt.py and mac/alt.py, so the shipped code is what is tested.
"""
import ast
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [os.path.join("windows", "alt.py"), os.path.join("mac", "alt.py")]
FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def check_raises(label, fn):
    try:
        fn()
    except SystemExit:
        print(f"  ok   {label}: rejected with a clear error")
        return
    print(f"  FAIL {label}: accepted (should have been rejected)")
    FAILURES.append(label)


def load_helpers(path, tmp):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, path)
    wanted = {"fail", "id_suffixes"}
    chunks = [ast.get_source_segment(src, n) for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in wanted]
    if len(chunks) != len(wanted):
        raise AssertionError(f"{path}: could not find fail/id_suffixes")
    ns = {"R": tmp, "os": os, "sys": sys, "time": time}
    exec("\n\n".join(chunks), ns)
    return src, ns["id_suffixes"]


def keep_for_suffixes(suffixes):
    return lambda rid: rid % 10 in suffixes


def keep_for_parity(parity):
    return lambda rid: rid % 2 == (0 if parity == "even" else 1)


def queue_name_for_suffixes(suffixes):
    return "/queue_ids_%s.json" % "".join(str(d) for d in suffixes)


def main():
    tmp = tempfile.mkdtemp(prefix="aqd-idfilter-")
    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        print(f"=== {rel}")
        src, id_suffixes = load_helpers(path, tmp)

        check("id_suffixes('1 3 5 7 9')", id_suffixes("1 3 5 7 9"), [1, 3, 5, 7, 9])
        check("id_suffixes('1,3,5')", id_suffixes("1,3,5"), [1, 3, 5])
        check("id_suffixes(' 9,1 ,3 ') trims and sorts", id_suffixes(" 9,1 ,3 "), [1, 3, 9])
        check("id_suffixes('2 4')", id_suffixes("2 4"), [2, 4])
        check("id_suffixes('') -> empty (parity fallback)", id_suffixes(""), [])
        check("id_suffixes(None) -> empty", id_suffixes(None), [])
        check("id_suffixes('1 1 2') de-duplicates", id_suffixes("1 1 2"), [1, 2])
        check("id_suffixes('0 9')", id_suffixes("0 9"), [0, 9])
        check_raises("id_suffixes('10') out of range", lambda: id_suffixes("10"))
        check_raises("id_suffixes('a') not a digit", lambda: id_suffixes("a"))
        check_raises("id_suffixes('1 -2') negative", lambda: id_suffixes("1 -2"))
        check_raises("id_suffixes('12,3') multi-char", lambda: id_suffixes("12,3"))

        check("source uses id % 10 in SUFFIXES", 'r["id"] % 10 in SUFFIXES' in src, True)
        check("source keeps the parity fallback", 'r["id"] % 2 == (0 if PARITY == "even" else 1)' in src, True)
        check("source names the suffix queue", '"/queue_ids_%s.json" % "".join(str(d) for d in SUFFIXES)' in src, True)
        check("source names the parity queue", 'MINE = R + f"/queue_{PARITY}.json"' in src, True)

        odd_keep = keep_for_suffixes(id_suffixes("1 3 5 7 9"))
        even_keep = keep_for_suffixes(id_suffixes("2 4"))
        odd_ids = [i for i in range(1, 201) if odd_keep(i)]
        even_ids = [i for i in range(1, 201) if even_keep(i)]
        check("suffix 1 3 5 7 9 -> last digits", sorted({i % 10 for i in odd_ids}), [1, 3, 5, 7, 9])
        check("suffix 2 4 -> last digits", sorted({i % 10 for i in even_ids}), [2, 4])
        check("suffix sets are disjoint", (set(odd_ids) & set(even_ids)), set())
        check("suffix 1 3 5 7 9 + 2 4 leaves 0,6,8 only",
              sorted({i % 10 for i in range(1, 201)} - {i % 10 for i in odd_ids} - {i % 10 for i in even_ids}),
              [0, 6, 8])
        check("every id goes to exactly one of the two machines",
              (len(odd_ids + even_ids), len(set(odd_ids) | set(even_ids)), set(odd_ids) & set(even_ids)),
              (len([i for i in range(1, 201) if i % 10 not in (0, 6, 8)]), 140, set()))

        check("queue name for 1 3 5 7 9", queue_name_for_suffixes(id_suffixes("1 3 5 7 9")), "/queue_ids_13579.json")
        check("queue name for 2 4", queue_name_for_suffixes(id_suffixes("2,4")), "/queue_ids_24.json")

        check("parity even", [i for i in range(1, 11) if keep_for_parity("even")(i)], [2, 4, 6, 8, 10])
        check("parity odd", [i for i in range(1, 11) if keep_for_parity("odd")(i)], [1, 3, 5, 7, 9])

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
