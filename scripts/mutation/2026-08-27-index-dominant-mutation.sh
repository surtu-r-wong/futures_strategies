#!/usr/bin/env bash
set -u
cd /home/elfbob/claude-code/futures_strategies
P=index_open_momentum/pg_source.py
C=common/minute/pg_source.py
BK=$(mktemp -d); cp $P $BK/p; cp $C $BK/c
restore(){ cp $BK/p $P; cp $BK/c $C; }
trap restore EXIT
py(){ python3 -c "
import pathlib,sys
p=pathlib.Path(sys.argv[1]); t=p.read_text(encoding='utf-8')
old,new=sys.argv[2],sys.argv[3]
assert old in t, 'PATTERN NOT FOUND: '+old[:70]
p.write_text(t.replace(old,new,1),encoding='utf-8')
" "$@"; }
run(){
  local label="$1" out failed n
  find common index_open_momentum tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  out=$(.venv/bin/python -m pytest tests/test_index_open_momentum_pg_source.py -q -p no:cacheprovider 2>&1)
  failed=$(printf '%s\n' "$out" | grep -oE '^FAILED [^ ]+::[A-Za-z0-9_]+' | sed 's/.*:://' | sort -u | tr '\n' ' ')
  n=$(printf '%s' "$failed" | wc -w)
  if printf '%s\n' "$out" | grep -q "error during collection\|^ERROR "; then echo "  ✅ $label —— 模块无法加载"
  elif [ "$n" -eq 0 ]; then echo "  ❌ $label —— 没有任何用例变红"
  else echo "  ✅ $label —— 打红 $n 个: $failed"; fi
  restore
}
echo "=== 基线 ==="; .venv/bin/python -m pytest tests/test_index_open_momentum_pg_source.py -q 2>&1 | tail -1
echo; echo "=== A. 主力选取 ==="
py $P "\nDOMINANT_SELECTION_LAG = 1\n" "\nDOMINANT_SELECTION_LAG = 0\n"
run "P1 滞后改 0（回看）"
py $P '["oi", "volume"], ascending=False' '["volume", "oi"], ascending=False'
run "P2 先按成交量排，持仓量退居其次"
py $P "                if int(best[\"oi\"]) == int(runner_up[\"oi\"]) and int(
                    best[\"volume\"]
                ) == int(runner_up[\"volume\"]):" "                if False:"
run "P3 去掉双平手硬失败"
py $P "            source_date = sessions[index - lag]" "            source_date = sessions[index]"
run "P4 用当日自己的持仓量选自己（回看）"
py $P "    if absent:" "    if False:"
run "P5 去掉品种缺席检查"
echo; echo "=== B. 对账 ==="
py $P "                None
                if (found := reference.get((choice.trade_date, choice.product))) is None
                else found == choice.contract" "                False
                if (found := reference.get((choice.trade_date, choice.product))) is None
                else found == choice.contract"
run "P6 无参照记成"不一致"而非"未知""
py $P "            reference_contract=reference.get((choice.trade_date, choice.product))," "            contract=reference.get((choice.trade_date, choice.product)) or choice.contract,
            reference_contract=reference.get((choice.trade_date, choice.product)),"
run "P7 分歧时静默改用参照的合约"
echo; echo "=== C. 时间窗 ==="
py $P "    ends = max(segment.end_minute for segment in rule.segments)" "    ends = min(segment.end_minute for segment in rule.segments)"
run "P8 窗口右端取最小段而非最大段"
py $P "    starts = min(segment.start_minute for segment in rule.segments)" "    starts = 570"
run "P9 开盘时刻写死 09:30（无视早年代）"
echo; echo "=== D. 尾部通路 ==="
py $P 'r"^(?:IF|IC|IH|IM)\d{4}(?:\.CFE)?$"' 'r"^(?:IF|IC|IH|IM)\d+(?:\.CFE)?$"'
run "P10 合约码判据放宽成"有数字就行""
py $P 'volume=("volume", "sum"), oi=("open_interest", "last")' 'volume=("volume", "sum"), oi=("open_interest", "sum")'
run "P11 持仓量当成流量求和"
py $P 'volume=("volume", "sum"), oi=("open_interest", "last")' 'volume=("volume", "last"), oi=("open_interest", "last")'
run "P12 成交量当成时点量取末根"
echo; echo "=== E. 共享层映射 ==="
py $C '    "CFE": "CFFEX",' '    "XXX": "CFFEX",'
run "P13 去掉 .CFE 交易所映射"
echo; echo "=== F. candidate 出处 ==="
py $P "                causal_in_pool_date=choice.selected_from," "                causal_in_pool_date=choice.trade_date,"
run "P14 candidate 记的依据日期改成当日"
echo; echo "=== 收尾 ==="; .venv/bin/python -m pytest tests/test_index_open_momentum_pg_source.py -q 2>&1 | tail -1
