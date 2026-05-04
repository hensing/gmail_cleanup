"""
Interactive terminal-based review UI for gmail-cleanup.

Shows classified emails grouped by sender cluster in a paginated table.
Users can accept, whitelist, or override categories at the cluster level,
then drill into individual emails for finer control.

No external dependencies — uses only print() and input() for maximum
compatibility inside a Docker TTY.

Key concepts:
- Cluster view: one row per sender cluster, sorted by email count descending.
- Drilldown view: all emails from one cluster, sorted by date descending.
- Whitelist: overrides any classification to KEEP, persisted across runs.
- Filter (cluster view): narrows cluster list to a single category.
- Filter (drilldown view): narrows email list by category or subject regex.
- Regex assign (rx): bulk-applies a category to emails whose subject matches
  a regex — globally in cluster view, per-cluster in drilldown.
"""

import json
import re
from typing import Optional

from config import Category
from db import DB

# Emails per page in both cluster and drilldown views
PAGE_SIZE = 20

# ANSI escape codes for terminal colours (works in any standard terminal)
_RESET = "\033[0m"
_BOLD = "\033[1m"
_COLORS = {
    "TRASH": "\033[91m",       # red
    "NEWSLETTER": "\033[93m",  # yellow
    "INVOICE": "\033[92m",     # green
    "ORDER": "\033[96m",       # cyan
    "KEEP": "\033[94m",        # blue
}


def _colored(text: str, category: str) -> str:
    return f"{_COLORS.get(category, '')}{text}{_RESET}"


def _truncate(s: str, n: int) -> str:
    """Truncate s to n characters, adding an ellipsis if truncated."""
    s = s or ""
    return s[:n - 1] + "…" if len(s) > n else s.ljust(n)


def _parse_cmd(raw: str) -> tuple[str, list[str]]:
    """Split user input into (command, arguments)."""
    parts = raw.strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def _filter_drill_emails(
    emails: list[dict], cat_filter: str, subject_filter: str
) -> list[dict]:
    """Apply category and/or subject-regex filter to a list of drilldown emails."""
    result = emails
    if cat_filter:
        result = [e for e in result if e["category"] == cat_filter]
    if subject_filter:
        try:
            pat = re.compile(subject_filter, re.IGNORECASE)
            result = [e for e in result if pat.search(e.get("subject") or "")]
        except re.error:
            pass
    return result


def _match_by_subject(emails: list[dict], pattern: str) -> list[dict]:
    """Return emails whose subject matches the regex; prints error and returns [] on bad pattern."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"  Invalid regex: {exc}")
        return []
    return [e for e in emails if rx.search(e.get("subject") or "")]


# ── cluster view ──────────────────────────────────────────────────────────────


def _print_cluster_table(clusters: list[dict], page: int, db: DB, active_filter: str = ""):
    """Render the paginated cluster table with header and one row per cluster."""
    total_pages = max(1, (len(clusters) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_slice = clusters[start:start + PAGE_SIZE]

    filter_str = f"  filter={_colored(active_filter, active_filter)}" if active_filter else ""
    print(f"\n{_BOLD}=== CLUSTER VIEW  —  page {page + 1}/{total_pages}, "
          f"{len(clusters)} clusters{_RESET}{filter_str}")

    hdr = f"{'':2} {'Nr':>4} │ {'Domain / Sender':<28} │ {'Mails':>5} │ {'Category':16} │ {'Distribution':<28} │ Contact"
    sep = "─" * len(hdr)
    print(hdr)
    print(sep)

    for i, cluster in enumerate(page_slice, start=start + 1):
        cat_counts = json.loads(cluster["cat_counts"] or "{}")
        total = cluster["mail_count"]
        dominant = cluster["override_cat"] or cluster["dominant_cat"]

        # Dominant category with percentage
        dom_count = cat_counts.get(dominant, total)
        pct = int(dom_count / total * 100) if total else 0
        cat_str = _colored(f"{dominant}({pct}%)", dominant)

        # Top-3 distribution
        dist_parts = sorted(cat_counts.items(), key=lambda x: -x[1])[:3]
        dist = " ".join(f"{k}:{v}" for k, v in dist_parts)

        # Display label: prefer specific address over domain
        label = _truncate(cluster["from_addr"] or cluster["from_domain"] or "", 28)

        # Contact indicator with sent count
        contact_str = ""
        addr = cluster["from_addr"] or ""
        if addr and db.is_contact(addr):
            cnt = db.get_contact_sent_count(addr)
            contact_str = f"✓ ({cnt}x)"

        # ✓ prefix for already-reviewed clusters
        reviewed_mark = "✓ " if cluster["reviewed"] else "  "
        print(f"{reviewed_mark}{i:>4} │ {label} │ {total:>5} │ {cat_str:<25} │ {_truncate(dist, 28)} │ {contact_str}")

    print()
    print(f"{_BOLD}Commands:{_RESET}  "
          "a <nr> accept  │  w <nr> whitelist  │  c <nr> CAT  │  "
          "d <nr> drilldown  │  f [CAT] filter  │  rx CAT <pattern>  │  "
          "n/p page  │  s summary  │  q quit")


# ── drilldown view ────────────────────────────────────────────────────────────


def _print_drilldown_table(
    cluster: dict,
    emails: list[dict],
    page: int,
    db: DB,
    active_filter: str = "",
):
    """Render the paginated email table for a single cluster."""
    label = cluster["from_addr"] or cluster["from_domain"] or "?"
    total_pages = max(1, (len(emails) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_slice = emails[start:start + PAGE_SIZE]

    filter_str = f"  filter={_BOLD}{active_filter}{_RESET}" if active_filter else ""
    print(f"\n{_BOLD}=== DRILLDOWN: {label}  —  {len(emails)} emails, "
          f"page {page + 1}/{total_pages} ==={_RESET}{filter_str}")

    hdr = f"{'':2} {'UID':>10} │ {'Date':12} │ {'Subject':<45} │ Category"
    print(hdr)
    print("─" * len(hdr))

    for e in page_slice:
        cat = e["category"]
        wl_mark = "W " if db.is_uid_whitelisted(e["uid"]) else "  "
        date_str = (e["date"] or "")[:12]
        subj = _truncate(e["subject"] or "(no subject)", 45)
        print(f"{wl_mark}{e['uid']:>10} │ {date_str:12} │ {subj} │ {_colored(cat, cat)}")

    print()
    print(f"{_BOLD}Commands:{_RESET}  "
          "w <uid> whitelist  │  wa whitelist all  │  ws whitelist sender  │  "
          "c <uid> CAT  │  f [CAT|regex] filter  │  rx CAT <pattern>  │  "
          "n/p page  │  b back")


# ── summary ───────────────────────────────────────────────────────────────────


def _print_summary(db: DB):
    stats = db.get_stats()
    cats = stats["categories"]
    total_cls = max(stats["total_classified"], 1)

    print(f"\n{_BOLD}=== SUMMARY ==={_RESET}")
    print(f"  Total emails:    {stats['total_emails']:>6}")
    print(f"  Classified:      {stats['total_classified']:>6}")
    print(f"  Known contacts:  {stats['total_contacts']:>6}")
    print()
    for cat in [c.value for c in Category]:
        count = cats.get(cat, 0)
        bar_len = min(30, count * 30 // total_cls)
        bar = "█" * bar_len
        print(f"  {_colored(f'{cat:<12}', cat)} {count:>6}  {bar}")


# ── main loop ─────────────────────────────────────────────────────────────────


def run_review(db: DB):
    """Start the interactive review session.

    The session has two modes that the user can switch between:
    - 'clusters':  paginated list of sender clusters
    - 'drilldown': paginated list of individual emails within one cluster

    Filters persist within each mode and are reset when leaving drilldown.
    """
    clusters = db.get_clusters()
    if not clusters:
        print("No clusters found. Run 'classify' first.")
        return

    mode = "clusters"
    cluster_page = 0
    drill_cluster_idx: Optional[int] = None
    drill_emails: list[dict] = []
    drill_page = 0
    drill_cat_filter = ""      # category filter active in drilldown
    drill_subject_filter = ""  # subject regex filter active in drilldown
    active_filter = ""
    all_clusters = clusters    # unfiltered reference

    _print_cluster_table(clusters, cluster_page, db, active_filter)

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting review.")
            break

        if not raw:
            continue

        cmd, args = _parse_cmd(raw)

        # ── cluster mode commands ─────────────────────────────────────────────
        if mode == "clusters":

            def _refresh_clusters():
                nonlocal clusters, all_clusters
                all_clusters = db.get_clusters()
                clusters = [c for c in all_clusters
                            if (c["override_cat"] or c["dominant_cat"]) == active_filter] \
                    if active_filter else all_clusters

            if cmd == "q":
                print("Review finished.")
                _print_summary(db)
                break

            elif cmd == "s":
                _print_summary(db)

            elif cmd == "f":
                valid_cats = [c.value for c in Category]
                new_filter = args[0].upper() if args else ""
                if new_filter and new_filter not in valid_cats:
                    print(f"  Unknown category. Valid: {', '.join(valid_cats)}")
                else:
                    active_filter = new_filter
                    cluster_page = 0
                    _refresh_clusters()
                    _print_cluster_table(clusters, cluster_page, db, active_filter)

            elif cmd == "rx":
                # rx <CATEGORY> <subject pattern...>
                if len(args) < 2:
                    print("  Usage: rx <CATEGORY> <subject pattern>")
                    print("  Example: rx TRASH Neue Nachricht von .*")
                    continue
                new_cat = args[0].upper()
                valid = [c.value for c in Category]
                if new_cat not in valid:
                    print(f"  Unknown category. Valid: {', '.join(valid)}")
                    continue
                pattern = " ".join(args[1:])
                all_emails = db.get_all_emails_for_regex()
                matches = _match_by_subject(all_emails, pattern)
                if not matches:
                    print(f"  No emails matched: {pattern}")
                    continue
                db.override_emails_batch([e["uid"] for e in matches], new_cat)
                _refresh_clusters()
                print(f"  ✓ {len(matches)} emails → {new_cat}  (pattern: {pattern})")
                _print_cluster_table(clusters, cluster_page, db, active_filter)

            elif cmd == "n":
                total_pages = max(1, (len(clusters) + PAGE_SIZE - 1) // PAGE_SIZE)
                if cluster_page < total_pages - 1:
                    cluster_page += 1
                _refresh_clusters()
                _print_cluster_table(clusters, cluster_page, db, active_filter)

            elif cmd == "p":
                if cluster_page > 0:
                    cluster_page -= 1
                _refresh_clusters()
                _print_cluster_table(clusters, cluster_page, db, active_filter)

            elif cmd in ("a", "w", "c", "d"):
                if not args:
                    print("  Missing cluster number. Example: a 3")
                    continue
                try:
                    nr = int(args[0])
                    idx = nr - 1
                    if idx < 0 or idx >= len(clusters):
                        print(f"  Invalid number. Valid range: 1–{len(clusters)}")
                        continue
                except ValueError:
                    print("  Expected a number.")
                    continue

                cluster = clusters[idx]
                cid = cluster["cluster_id"]

                if cmd == "a":
                    db.mark_cluster_reviewed(cid)
                    _refresh_clusters()
                    print(f"  ✓ Cluster #{nr} accepted ({cluster['dominant_cat']})")
                    _print_cluster_table(clusters, cluster_page, db, active_filter)

                elif cmd == "w":
                    db.add_to_whitelist(cluster_id=cid)
                    _refresh_clusters()
                    print(f"  ✓ Cluster #{nr} whitelisted (all emails → KEEP)")
                    _print_cluster_table(clusters, cluster_page, db, active_filter)

                elif cmd == "c":
                    if len(args) < 2:
                        print("  Usage: c <nr> <CATEGORY>")
                        continue
                    new_cat = args[1].upper()
                    valid = [c.value for c in Category]
                    if new_cat not in valid:
                        print(f"  Unknown category. Valid: {', '.join(valid)}")
                        continue
                    db.override_cluster(cid, new_cat)
                    _refresh_clusters()
                    print(f"  ✓ Cluster #{nr} → {new_cat}")
                    _print_cluster_table(clusters, cluster_page, db, active_filter)

                elif cmd == "d":
                    drill_cluster_idx = idx
                    drill_emails = db.get_cluster_emails(cid)
                    drill_page = 0
                    drill_cat_filter = ""
                    drill_subject_filter = ""
                    mode = "drilldown"
                    visible = _filter_drill_emails(drill_emails, drill_cat_filter, drill_subject_filter)
                    _print_drilldown_table(cluster, visible, drill_page, db)

            else:
                # Redraw on any unrecognised input
                _print_cluster_table(clusters, cluster_page, db, active_filter)

        # ── drilldown mode commands ───────────────────────────────────────────
        elif mode == "drilldown":
            cluster = clusters[drill_cluster_idx]
            cid = cluster["cluster_id"]

            def _drill_show():
                """Recompute visible list and redraw drilldown."""
                visible = _filter_drill_emails(drill_emails, drill_cat_filter, drill_subject_filter)
                flabel = drill_cat_filter or drill_subject_filter
                _print_drilldown_table(cluster, visible, drill_page, db, flabel)

            if cmd == "b":
                drill_cat_filter = ""
                drill_subject_filter = ""
                mode = "clusters"
                _refresh_clusters()
                _print_cluster_table(clusters, cluster_page, db, active_filter)

            elif cmd == "f":
                if not args:
                    # Clear all drilldown filters
                    drill_cat_filter = ""
                    drill_subject_filter = ""
                    drill_page = 0
                else:
                    arg0 = args[0].upper()
                    valid_cats = [c.value for c in Category]
                    if arg0 in valid_cats:
                        drill_cat_filter = arg0
                        drill_subject_filter = ""
                    else:
                        # Treat entire arg string as subject regex
                        drill_subject_filter = " ".join(args)
                        drill_cat_filter = ""
                    drill_page = 0
                _drill_show()

            elif cmd == "rx":
                # rx <CATEGORY> <subject pattern...>  — applies within current cluster
                if len(args) < 2:
                    print("  Usage: rx <CATEGORY> <subject pattern>")
                    print("  Example: rx KEEP Neue Nachricht von .*")
                    continue
                new_cat = args[0].upper()
                valid = [c.value for c in Category]
                if new_cat not in valid:
                    print(f"  Unknown category. Valid: {', '.join(valid)}")
                    continue
                pattern = " ".join(args[1:])
                matches = _match_by_subject(drill_emails, pattern)
                if not matches:
                    print(f"  No emails matched: {pattern}")
                    continue
                db.override_emails_batch([e["uid"] for e in matches], new_cat)
                drill_emails = db.get_cluster_emails(cid)
                _refresh_clusters()
                print(f"  ✓ {len(matches)} emails → {new_cat}  (pattern: {pattern})")
                _drill_show()

            elif cmd == "n":
                visible = _filter_drill_emails(drill_emails, drill_cat_filter, drill_subject_filter)
                total_pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
                if drill_page < total_pages - 1:
                    drill_page += 1
                _drill_show()

            elif cmd == "p":
                if drill_page > 0:
                    drill_page -= 1
                _drill_show()

            elif cmd == "wa":
                db.add_to_whitelist(cluster_id=cid)
                drill_emails = db.get_cluster_emails(cid)
                _refresh_clusters()
                label = cluster["from_addr"] or cluster["from_domain"] or "?"
                print(f"  ✓ All emails from '{label}' whitelisted → KEEP")
                _drill_show()

            elif cmd == "ws":
                addr = cluster["from_addr"]
                if not addr:
                    print("  Domain-level cluster — use 'wa' to whitelist all, or drill into a specific address.")
                    continue
                db.whitelist_sender(addr)
                drill_emails = db.get_cluster_emails(cid)
                _refresh_clusters()
                print(f"  ✓ Sender '{addr}' added to contacts → always KEEP")
                _drill_show()

            elif cmd == "w":
                if not args:
                    print("  Usage: w <uid>  │  wa = whitelist all  │  ws = whitelist sender")
                    continue
                try:
                    uid = int(args[0])
                except ValueError:
                    print("  Expected a UID number.")
                    continue
                db.add_to_whitelist(uid=uid)
                drill_emails = db.get_cluster_emails(cid)
                print(f"  ✓ UID {uid} whitelisted")
                _drill_show()

            elif cmd == "c":
                if len(args) < 2:
                    print("  Usage: c <uid> <CATEGORY>")
                    continue
                try:
                    uid = int(args[0])
                except ValueError:
                    print("  Expected a UID number.")
                    continue
                new_cat = args[1].upper()
                valid = [c.value for c in Category]
                if new_cat not in valid:
                    print(f"  Unknown category. Valid: {', '.join(valid)}")
                    continue
                db.override_email(uid, new_cat)
                drill_emails = db.get_cluster_emails(cid)
                print(f"  ✓ UID {uid} → {new_cat}")
                _drill_show()

            else:
                _drill_show()
