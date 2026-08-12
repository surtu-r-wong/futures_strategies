# Convert archive minute CSVs into PostgreSQL COPY text format, one file per year.
#
# Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
#
# Archive layout (12 or 13 columns; 2025 headers carry a UTF-8 BOM):
#   1 exchange  2 symbol   3 open      4 close  5 high  6 low
#   7 amount    8 volume   9 position  10 bob   11 eob  12 type [13 sequence]
#
# Emitted column order matches
#   COPY public.futures_minute
#       (bar_time, symbol, exchange, open, high, low, close,
#        volume, amount, open_interest)
#
# Note open/close/high/low are NOT in that order in the source.
#
# Input arrives as every CSV concatenated into one stream, so file boundaries
# are recognised by the header line rather than by FNR. That keeps the whole
# run in a single process, which is what makes routing with `>` correct: awk
# truncates a redirect target once per process and appends thereafter.
#
# Per-file dedup is sufficient because a (symbol, bar_time) key never spans
# files: the 2005-2024 packages stop at 2024-12 and the 2025/2026 packages hold
# one symbol-day each, so the two never overlap.
#
# Variables:
#   OUTDIR  route rows to OUTDIR/<year>.tsv; unset writes to stdout

BEGIN { FS = ","; OFS = "\t" }

# Header line, also the file boundary. Matching on content rather than position
# tolerates the UTF-8 BOM the 2025 files carry.
/exchange,symbol,open,close/ { delete seen; files++; next }

NF < 10 { bad++; next }

# Vendor continuous series (9999 main, 9998, 8888 weighted). Present only from
# 2025 onward, so admitting them would yield a series that silently starts in
# 2025; the project generates its own continuous contracts under documented
# rules instead. Contract months are YYMM, so these never collide with a real
# contract.
$2 ~ /(9999|9998|8888|0000)$/ { cont++; next }

{
    key = $2 SUBSEP $10
    if (key in seen) { dup++; next }
    seen[key] = 1

    year = substr($10, 1, 4)
    line = $10 OFS toupper($2) OFS $1 OFS num($3) OFS num($5) OFS num($6) \
           OFS num($4) OFS num($8) OFS num($7) OFS num($9)

    if (OUTDIR != "") print line > (OUTDIR "/" year ".tsv")
    else print line

    kept++
    rows[year]++
}

function num(v) { return (v == "" || v == "nan" || v == "NaN") ? "\\N" : v }

END {
    for (y in rows) printf("YEAR\t%s\t%d\n", y, rows[y]) > "/dev/stderr"
    printf("TOTAL\tfiles=%d kept=%d dup=%d continuous=%d malformed=%d\n",
           files, kept, dup, cont, bad) > "/dev/stderr"
}
