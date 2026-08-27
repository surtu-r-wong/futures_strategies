#!/usr/bin/env bash
# 变异验证：逐条改坏被测逻辑，确认预期用例变红。
# 验收 = "打红集合 == 预期集合"，不是"有测试红了就算数"。
set -u
cd /home/elfbob/claude-code/futures_strategies
SESS=common/minute/sessions.py
IDX=index_open_momentum/sessions.py
CSV=config/index_minute_sessions.csv
BK=$(mktemp -d)
cp $SESS $BK/s; cp $IDX $BK/i; cp $CSV $BK/c
restore() { cp $BK/s $SESS; cp $BK/i $IDX; cp $BK/c $CSV; }
trap restore EXIT

py() { python3 -c "
import pathlib,sys
p=pathlib.Path(sys.argv[1]); t=p.read_text(encoding='utf-8')
old,new=sys.argv[2],sys.argv[3]
assert old in t, 'PATTERN NOT FOUND: '+old[:60]
p.write_text(t.replace(old,new,1),encoding='utf-8')
" "$@"; }

run() {
  local label="$1"
  local out failed n
  # ⚠️ 必须清 __pycache__：等长变异（915→914）+ 同秒 mtime 会让 .pyc 失效判据两项都不变，
  # Python 直接用缓存字节码，变异根本没进过解释器 —— 会假报「测试没抓住」。
  find common index_open_momentum tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  out=$(.venv/bin/python -m pytest tests/test_index_open_momentum_sessions.py -q -p no:cacheprovider 2>&1)
  failed=$(printf '%s\n' "$out" | grep -oE '^FAILED [^ ]+::[A-Za-z0-9_]+' | sed 's/.*:://' | sort -u | tr '\n' ' ')
  n=$(printf '%s' "$failed" | wc -w)
  if printf '%s\n' "$out" | grep -q "error during collection\|ImportError\|^ERROR "; then
    echo "  ✅ $label —— 整个模块无法加载（构造期即炸）"
  elif [ "$n" -eq 0 ]; then
    echo "  ❌ $label —— 没有任何用例变红"
  else
    echo "  ✅ $label —— 打红 $n 个: $failed"
  fi
  restore
}

echo "=== 基线（不变异，应全绿）==="
.venv/bin/python -m pytest tests/test_index_open_momentum_sessions.py -q 2>&1 | tail -1

echo; echo "=== A. ruleset ==="
py $SESS "                SessionSegment(555, 690)," "                SessionSegment(570, 690),"
run "M1 早年代开盘 09:15→09:30（抹掉年代差异）"

py $SESS "                SessionSegment(780, 900)," "                SessionSegment(780, 915),"
run "M2 晚年代收盘 15:00→15:15"

py $SESS "            date(2016, 1, 1)," "            date(2017, 1, 1),"
run "M3 年代切换日 2016-01-01→2017-01-01"

py $SESS "    clock_end_minute=915,
    allows_night=False," "    clock_end_minute=900,
    allows_night=False,"
run "M4 时钟上界 915→900（收窄，2016 前日盘应构造不出）"

py $SESS "    allows_night=False,
)" "    allows_night=True,
)"
run "M5 CFFEX 改成允许夜盘"

echo; echo "=== B. 已知缺口 ==="
py $IDX "        end_minute=915," "        end_minute=914,"
run "M6 缺口右端 915→914（少登记一分钟）"

py $IDX "        effective_end=date(2015, 12, 31)," "        effective_end=date(2016, 12, 31),"
run "M7 缺口生效期延到 2016 年底"

py $IDX "            \"本库 2016 前每个交易日最后一根 bar 是 14:59，而中金所当时交易至 15:15：\"" "            \"档案有缺口。\""
run "M8 缺口理由抽成一句空话"

echo; echo "=== C. 映射硬失败 ==="
py $IDX "        if bar_time in seen:" "        if False:"
run "M9 去掉重复时间戳检查"

py $IDX "        if bar_time.tzinfo is None or bar_time.utcoffset() is None:" "        if False:"
run "M10 去掉 naive 时间戳检查"

py $IDX "        if bar_time not in allowed:" "        if False:"
run "M11 去掉'必须落在 slot 上'检查"

echo; echo "=== D. 覆盖度闸 ==="
py $IDX "paper_faithful=worst <= max_missing_days," "paper_faithful=worst < max_missing_days,"
run "M12 闸门 <= 改成 <（fail-closed 边界偏一格）"

py $IDX "        expected = {day for day in calendar if day >= listed}" "        expected = set(calendar)"
run "M13 忽略挂牌日（拿全窗口当分母）"

py $IDX "        if stray:" "        if False:"
run "M14 去掉'观测日必须在日历内'检查"

echo; echo "=== E. 资产 ==="
sed -i '/CFFEX,IM,/d' $CSV
run "M15 资产删掉 IM"

sed -i 's/CFFEX,IF,2010-04-16,2015-12-31,/CFFEX,IF,2010-04-16,2016-12-31,/' $CSV
run "M16 IF 早年代生效期与晚年代重叠"

sed -i 's/CFFEX,IC,2015-04-16,/CFFEX,IC,2015-01-05,/' $CSV
run "M17 IC 挂牌日提前到 2015-01-05"

echo; echo "=== 收尾：确认已还原 ==="
.venv/bin/python -m pytest tests/test_index_open_momentum_sessions.py -q 2>&1 | tail -1
