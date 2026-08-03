#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the publications list for the Rock Laboratory site from ORCID.

The ORCID public API is the source of truth for what has been published.
It does not, however, carry volume / issue / page numbers, so each record
that has a DOI is enriched from Crossref. Works that never made it into
ORCID (older local conference papers, for instance) are kept by hand in
scripts/extra_publications.json and merged in.

Nothing is written unless the whole run succeeds: a failing API call exits
non-zero and leaves the existing files untouched.

Usage:
    python scripts/fetch_orcid.py              # fetch, then write the files
    python scripts/fetch_orcid.py --dry-run    # fetch and print, write nothing
    python scripts/fetch_orcid.py --no-crossref
"""

from __future__ import print_function

import argparse
import io
import json
import os
import sys
import time

try:                                      # Python 3
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
except ImportError:                       # Python 2
    from urllib2 import Request, urlopen, URLError, HTTPError


# --------------------------------------------------------------------------
# Settings -- edit these
# --------------------------------------------------------------------------

ORCID_ID = "0000-0001-6519-4238"

# Shown to Crossref so the request lands in their "polite" pool.
CONTACT_EMAIL = "ccchiu@ntut.edu.tw"

# Surnames to emphasise in the author list (lab members).
HIGHLIGHT_NAMES = ["Chiu"]

# Marker comments in publications.html between which the list is injected.
BEGIN_MARKER = "<!-- ORCID:BEGIN -->"
END_MARKER = "<!-- ORCID:END -->"

REQUEST_TIMEOUT = 30      # seconds
REQUEST_PAUSE = 0.2       # seconds between calls, to stay polite

ORCID_API = "https://pub.orcid.org/v3.0"
CROSSREF_API = "https://api.crossref.org/works"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRA_PATH = os.path.join(REPO_ROOT, "scripts", "extra_publications.json")

# The list is rendered once per language: same works, translated headings and
# link labels. Each fragment is written out on its own and injected into the
# publications page of that language. The PI page deliberately links here
# rather than repeating all 66 entries.
OUTPUTS = {
    "zh": {
        "fragment": os.path.join(REPO_ROOT, "publications-list.html"),
        "pages": [os.path.join(REPO_ROOT, "publications.html")],
    },
    "en": {
        "fragment": os.path.join(REPO_ROOT, "publications-list-en.html"),
        "pages": [os.path.join(REPO_ROOT, "en", "publications.html")],
    },
}

# ORCID work types -> the section they belong in.
#
# book / book-chapter sit with the conference papers on purpose: in this record
# they are all proceedings volumes (Springer LNCE, GeoChina) that ORCID and
# Crossref happen to type as books. Entries in extra_publications.json can use
# the pseudo-types "domestic-journal" and "thesis", which ORCID has no notion of.
SECTIONS = [
    ("journal", {"zh": u"國際期刊論文", "en": u"International Journal Papers"},
     {"journal-article", "review", "translation"}),
    ("domestic", {"zh": u"國內期刊論文", "en": u"Domestic Journal Papers"},
     {"domestic-journal"}),
    ("conference", {"zh": u"研討會論文", "en": u"Conference Papers"},
     {"conference-paper", "conference-abstract", "conference-poster",
      "book", "book-chapter"}),
    ("thesis", {"zh": u"學位論文", "en": u"Theses"},
     {"thesis", "dissertation-thesis"}),
    ("other", {"zh": u"其他", "en": u"Other"},
     {"report", "supervised-student-publication"}),
]

LINK_LABEL = {"zh": u"連結", "en": u"link"}
DEFAULT_SECTION = "other"


class FetchError(Exception):
    """Raised when the run cannot produce a trustworthy list."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def get_json(url, accept="application/json"):
    request = Request(url, headers={
        "Accept": accept,
        "User-Agent": "RockLab-site/1.0 (+https://github.com/jatestrella/RockLab; mailto:%s)" % CONTACT_EMAIL,
    })
    try:
        response = urlopen(request, timeout=REQUEST_TIMEOUT)
    except HTTPError as exc:
        raise FetchError("HTTP %s from %s" % (exc.code, url))
    except URLError as exc:
        raise FetchError("could not reach %s (%s)" % (url, exc.reason))
    try:
        payload = response.read().decode("utf-8")
    finally:
        response.close()
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise FetchError("invalid JSON from %s (%s)" % (url, exc))


# --------------------------------------------------------------------------
# ORCID
# --------------------------------------------------------------------------

def dig(node, *keys):
    """Walk nested dicts, tolerating the nulls ORCID sprinkles everywhere."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def summary_put_codes(orcid_id):
    data = get_json("%s/%s/works" % (ORCID_API, orcid_id))
    groups = data.get("group") or []
    if not groups:
        raise FetchError("ORCID returned no works for %s" % orcid_id)

    put_codes = []
    for group in groups:
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        # A group collects duplicates of one work from different sources;
        # the first entry is ORCID's preferred version.
        put_codes.append(summaries[0]["put-code"])
    return put_codes


def external_ids(work):
    found = {}
    for entry in dig(work, "external-ids", "external-id") or []:
        id_type = (entry.get("external-id-type") or "").lower()
        value = entry.get("external-id-value")
        if id_type and value and id_type not in found:
            found[id_type] = value
    return found


def author_names(work):
    names = []
    for contributor in dig(work, "contributors", "contributor") or []:
        role = dig(contributor, "contributor-attributes", "contributor-role")
        if role and role.lower() not in ("author", "first-author"):
            continue
        name = dig(contributor, "credit-name", "value")
        if name:
            names.append(name.strip())
    return names


def fetch_work(orcid_id, put_code):
    work = get_json("%s/%s/work/%s" % (ORCID_API, orcid_id, put_code))
    ids = external_ids(work)

    year = dig(work, "publication-date", "year", "value")
    month = dig(work, "publication-date", "month", "value")

    return {
        "title": (dig(work, "title", "title", "value") or "").strip(),
        "container": (dig(work, "journal-title", "value") or "").strip(),
        "type": work.get("type") or "",
        "year": int(year) if year and year.isdigit() else None,
        "month": int(month) if month and month.isdigit() else None,
        "doi": ids.get("doi"),
        "url": dig(work, "url", "value"),
        "authors": author_names(work),
        "volume": None,
        "issue": None,
        "pages": None,
        "source": "orcid",
    }


# --------------------------------------------------------------------------
# Crossref enrichment (volume / issue / pages, which ORCID does not carry)
# --------------------------------------------------------------------------

def enrich_from_crossref(record):
    doi = record.get("doi")
    if not doi:
        return
    try:
        message = get_json("%s/%s" % (CROSSREF_API, doi)).get("message") or {}
    except FetchError as exc:
        # Supplementary data only -- a miss must not sink the run.
        warn("Crossref lookup failed for %s (%s)" % (doi, exc))
        return

    record["volume"] = message.get("volume") or record["volume"]
    record["issue"] = message.get("issue") or record["issue"]
    record["pages"] = message.get("page") or record["pages"]

    if not record["container"]:
        titles = message.get("container-title") or []
        if titles:
            record["container"] = titles[0]

    if not record["authors"]:
        names = []
        for author in message.get("author") or []:
            name = ("%s %s" % (author.get("given", ""), author.get("family", ""))).strip()
            if name:
                names.append(name)
        record["authors"] = names

    if not record["year"]:
        parts = dig(message, "issued", "date-parts")
        if parts and parts[0]:
            record["year"] = parts[0][0]


# --------------------------------------------------------------------------
# Manually maintained extras
# --------------------------------------------------------------------------

def load_extras():
    if not os.path.exists(EXTRA_PATH):
        return []
    with io.open(EXTRA_PATH, encoding="utf-8") as handle:
        try:
            entries = json.load(handle)
        except ValueError as exc:
            raise FetchError("%s is not valid JSON (%s)" % (EXTRA_PATH, exc))

    records = []
    for entry in entries:
        record = {
            "title": entry.get("title", "").strip(),
            "container": entry.get("container", "").strip(),
            "type": entry.get("type", "conference-paper"),
            "year": entry.get("year"),
            "month": entry.get("month"),
            "doi": entry.get("doi"),
            "url": entry.get("url"),
            "authors": entry.get("authors") or [],
            "volume": entry.get("volume"),
            "issue": entry.get("issue"),
            "pages": entry.get("pages"),
            "source": "manual",
        }
        if not record["title"]:
            raise FetchError("an entry in %s has no title" % EXTRA_PATH)
        records.append(record)
    return records


def normalise(title):
    return "".join(ch.lower() for ch in title if ch.isalnum())


def merge(orcid_records, extra_records):
    """ORCID wins; extras fill gaps ORCID does not know about."""
    seen = set(normalise(r["title"]) for r in orcid_records)
    merged = list(orcid_records)
    for record in extra_records:
        if normalise(record["title"]) in seen:
            continue
        seen.add(normalise(record["title"]))
        merged.append(record)
    return merged


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def escape(text):
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def has_cjk(text):
    """True if the string carries Han characters."""
    for ch in text:
        if u"㐀" <= ch <= u"鿿" or u"豈" <= ch <= u"﫿":
            return True
    return False


def section_key(work_type):
    for key, _labels, types in SECTIONS:
        if work_type in types:
            return key
    return DEFAULT_SECTION


def format_authors(names):
    if not names:
        return ""
    parts = []
    for name in names:
        marked = escape(name)
        for highlight in HIGHLIGHT_NAMES:
            if highlight.lower() in name.lower():
                marked = "<u>%s</u>" % marked
                break
        parts.append(marked)
    if len(parts) == 1:
        return parts[0]
    if any(has_cjk(name) for name in names):
        # Chinese-language works list their authors with the ideographic
        # comma and no conjunction.
        return u"、".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def format_locator(record):
    """volume(issue), pages -- whichever of them exist."""
    bits = []
    if record.get("volume"):
        volume = escape(str(record["volume"]))
        if record.get("issue"):
            volume += "(%s)" % escape(str(record["issue"]))
        bits.append(volume)
    if record.get("pages"):
        bits.append(escape(str(record["pages"])))
    return ", ".join(bits)


def format_citation(record, lang):
    title = record["title"]
    container = record["container"]
    locator = format_locator(record)

    # Chinese-language works take full-width punctuation and no spacing.
    chinese = has_cjk(title) or any(has_cjk(a) for a in record["authors"])
    # Synthesised italics on Han characters look broken, so only Latin
    # container names are emphasised.
    venue = escape(container) if has_cjk(container) else "<em>%s</em>" % escape(container)

    pieces = []
    authors = format_authors(record["authors"])

    if chinese:
        if authors:
            pieces.append(authors)
        pieces.append(escape(title).rstrip(u"。."))
        if container:
            pieces.append(venue)
        if locator:
            pieces.append(locator)
        citation = u"，".join(pieces) + u"。"
    else:
        if authors:
            # Credit names are often already abbreviated ("Weng, M.C."), and the
            # rendered string may end in </u>, so test the raw name instead.
            trailing = "" if record["authors"][-1].rstrip().endswith(".") else "."
            pieces.append(authors + trailing)
        pieces.append(escape(title).rstrip(".") + ".")
        if container:
            pieces.append(venue + ("," if locator else "."))
        if locator:
            pieces.append(locator + ".")
        citation = " ".join(pieces)

    if record.get("doi"):
        doi = escape(record["doi"])
        citation += ' <a href="https://doi.org/%s">doi:%s</a>' % (doi, doi)
    elif record.get("url"):
        citation += ' <a href="%s">%s</a>' % (escape(record["url"]),
                                                LINK_LABEL[lang])
    return citation


def sort_key(record):
    return (-(record["year"] or 0), -(record["month"] or 0), record["title"].lower())


def render(records, generated_at, lang):
    buckets = {}
    for record in records:
        buckets.setdefault(section_key(record["type"]), []).append(record)

    lines = [
        "<!-- Generated by scripts/fetch_orcid.py from ORCID %s on %s." % (ORCID_ID, generated_at),
        "     Do not edit by hand; your changes will be overwritten.",
        "     Works missing from ORCID belong in scripts/extra_publications.json. -->",
    ]

    for key, labels, _types in SECTIONS:
        entries = sorted(buckets.get(key, []), key=sort_key)
        if not entries:
            continue
        lines.append("")
        lines.append("\t\t\t\t\t\t\t\t<section>")
        lines.append("\t\t\t\t\t\t\t\t\t<h2>%s</h2>" % labels[lang])
        lines.append("\t\t\t\t\t\t\t\t\t<ol class=\"pubs\">")
        for record in entries:
            year = str(record["year"]) if record["year"] else "—"
            lines.append(
                "\t\t\t\t\t\t\t\t\t\t<li><span class=\"year\">%s</span>"
                "<span class=\"body\">%s</span></li>" % (year, format_citation(record, lang))
            )
        lines.append("\t\t\t\t\t\t\t\t\t</ol>")
        lines.append("\t\t\t\t\t\t\t\t</section>")

    return "\n".join(lines) + "\n"


def inject(page_text, fragment, path):
    start = page_text.find(BEGIN_MARKER)
    end = page_text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise FetchError(
            "could not find %s / %s markers in %s" % (BEGIN_MARKER, END_MARKER, path))
    head = page_text[:start + len(BEGIN_MARKER)]
    tail = page_text[end:]
    return head + "\n" + fragment + "\n\t\t\t\t\t\t\t\t" + tail


# --------------------------------------------------------------------------

def warn(message):
    sys.stderr.write("warning: %s\n" % message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the fragment instead of writing any file")
    parser.add_argument("--no-crossref", action="store_true",
                        help="skip the Crossref lookup for volume/issue/pages")
    args = parser.parse_args()

    print("Fetching works for ORCID %s ..." % ORCID_ID)
    put_codes = summary_put_codes(ORCID_ID)
    print("  %d work groups" % len(put_codes))

    records = []
    for index, put_code in enumerate(put_codes, 1):
        record = fetch_work(ORCID_ID, put_code)
        if not record["title"]:
            warn("put-code %s has no title; skipped" % put_code)
            continue
        records.append(record)
        print("  [%2d/%d] %s" % (index, len(put_codes), record["title"][:66]))
        time.sleep(REQUEST_PAUSE)

    if not records:
        raise FetchError("ORCID returned works but none had a usable title")

    if not args.no_crossref:
        print("Enriching from Crossref ...")
        for record in records:
            if record["doi"]:
                enrich_from_crossref(record)
                time.sleep(REQUEST_PAUSE)

    extras = load_extras()
    if extras:
        print("Merging %d manually maintained entries ..." % len(extras))
    records = merge(records, extras)

    generated_at = time.strftime("%Y-%m-%d", time.gmtime())
    fragments = dict((lang, render(records, generated_at, lang)) for lang in OUTPUTS)

    if args.dry_run:
        for lang, fragment in sorted(fragments.items()):
            print("\n----- %s (dry run) -----\n"
                  % os.path.basename(OUTPUTS[lang]["fragment"]))
            sys.stdout.write(fragment)
        print("\n----- %d works, nothing written -----" % len(records))
        return 0

    # Render every target before writing anything, so a missing marker in a
    # later page cannot leave an earlier one half-updated.
    rendered = {}
    for lang, cfg in OUTPUTS.items():
        for path in cfg["pages"]:
            if not os.path.exists(path):
                raise FetchError("target page %s does not exist" % path)
            with io.open(path, encoding="utf-8") as handle:
                rendered[path] = inject(handle.read(), fragments[lang], path)

    touched = []
    for lang, cfg in OUTPUTS.items():
        with io.open(cfg["fragment"], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(fragments[lang])
        touched.append(cfg["fragment"])
    for path, text in rendered.items():
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        touched.append(path)

    print("Updated %d files with %d works:" % (len(touched), len(records)))
    for path in sorted(touched):
        print("  " + os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as error:
        sys.stderr.write("error: %s\n" % error)
        sys.stderr.write("existing files were left unchanged.\n")
        sys.exit(1)
