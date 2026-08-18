# Carry 分钟时段权威来源登记

日期：2026-08-14
覆盖目标：2011-01-01 至 2026-04-29
资产版本：`commodity-v1`

本文只登记交易所官方域名上的材料。`rows_derived` 是来源层可派生的候选行数，
不是已经批准写入 CSV 的行数。所有节前停夜盘材料先保留公告中的自然日
`notice_evening`；只有用完整全局交易日历映射后，才会生成
`carry_minute_session_exceptions.csv.trade_date`。当前登记不以经验缺行反推制度。

年度休市公告中的 `E→R` 表示公告明确的节前停夜盘自然日 `E` 与公告所称
“`R` 日起照常开市”的恢复日期；专项连续交易通知则明确写作“`R` 日当晚恢复
连续交易”。采集时只把 `E` 保存为 `notice_evening`，并以完整全局目标交易日历
计算 `trade_date = min(global_target_trade_date > E)`。`R` 只用于边界复核，不能
直接当作 `trade_date`，也不能把 `E` 本身写入 CSV。2020 年春节延长公告只修订
恢复开市边界，没有重述停夜盘前夕，因此 `rows_derived=0`。

## 来源登记

| exchange | authority_kind | covered_dates_or_regime | source_url | rows_derived | review_status |
|---|---|---|---|---:|---|
| SHFE | fu_history | 2011-01-14 公布旧 FU 合约修订；FU1202 起采用修订合约，旧新合约过渡至 2011-11-30 | https://www.shfe.com.cn/docview/docview_10141355.htm | 0 | `reviewed_history_only`：只支持 FU 沿革，不支持夜盘时段 |
| SHFE | fu_history_and_launch | 2018-06-27 起终止旧 FU；保税 380CST FU 于 2018-07-16 上市，通知列明 21:00–23:00 | https://www.shfe.com.cn/publicnotice/notice/201806/t20180626_793285.html | 1 | `reviewed_direct`：历史事实与新合约时段均直接列明；不得回填到旧 FU |
| SHFE | session_launch | AU、AG 自 2013-07-05 开展连续交易，21:00–02:30；同时列明节前夜盘排除规则 | https://www.shfe.com.cn/publicnotice/notice/201306/t20130607_789661.html | 2 | `reviewed_direct` |
| SHFE | session_launch | CU、AL、ZN、PB 自 2013-12-20 开展连续交易，21:00–01:00 | https://www.shfe.com.cn/docview/docview_310288380.htm | 4 | `reviewed_corroborative`：交易所域名转载报道，尚未恢复上期办发〔2013〕151号原通知 |
| SHFE | session_launch | RB、HC、BU 自 2014-12-26 为 21:00–01:00，RU 为 21:00–23:00 | https://www.shfe.com.cn/content/lxjy/mtbd6.html | 4 | `reviewed_corroborative`：事实直接列明，尚未恢复上期发〔2014〕173号原通知 |
| SHFE | session_clock_change | 自 2016-05-03 晚起，RB、HC、BU 从 01:00 调整至 23:00 | https://www.shfe.com.cn/publicnotice/notice/201604/t20160426_791532.html | 3 | `reviewed_direct` |
| SHFE | session_launch | NI、SN 于 2015-03-27 上市，夜盘 21:00–01:00 | https://www.shfe.com.cn/publicnotice/notice/201503/t20150320_790883.html | 2 | `reviewed_direct` |
| SHFE | session_launch | SP 于 2018-11-27 上市，夜盘 21:00–23:00 | https://www.shfe.com.cn/publicnotice/notice/201811/t20181107_793841.html | 1 | `reviewed_direct` |
| SHFE | session_launch | SS 于 2019-09-25 上市，夜盘 21:00–01:00 | https://www.shfe.com.cn/publicnotice/notice/201909/t20190923_795071.html | 1 | `reviewed_direct` |
| SHFE | session_launch | AO 于 2023-06-19 上市，夜盘 21:00–01:00 | https://www.shfe.com.cn/publicnotice/notice/202306/t20230606_800293.html | 1 | `reviewed_direct` |
| SHFE | session_launch | BR 于 2023-07-28 上市，夜盘 21:00–23:00 | https://www.shfe.com.cn/publicnotice/notice/202307/t20230718_800481.html | 1 | `reviewed_direct` |
| SHFE | session_launch | AD 于 2025-06-10 上市，夜盘 21:00–01:00 | https://www.shfe.com.cn/publicnotice/notice/202505/t20250526_827862.html | 1 | `reviewed_direct` |
| SHFE | session_launch | OP 于 2025-09-10 上市，夜盘 21:00–23:00 | https://www.shfe.com.cn/publicnotice/notice/202508/t20250818_828697.html | 1 | `reviewed_direct` |
| SHFE | night_suspension | 自 2020-02-03 晚起暂停夜盘，恢复日期另行通知 | https://www.shfe.com.cn/publicnotice/notice/202002/t20200202_795584.html | 0 | `reviewed_boundary`：与恢复边界及全局日历已展开 63 个目标日 |
| SHFE | night_resumption | 2020-05-06 晚恢复夜盘 | https://www.shfe.com.cn/publicnotice/notice/202004/t20200424_795871.html | 0 | `reviewed_boundary`：闭合暂停区间，不重复计行 |
| SHFE | holiday_no_night | 《关于2013年中秋节和国庆节期间连续交易时间安排的通知》（上期发〔2013〕140号，2013-09-10）；`E→R`：2013-09-18→09-23、2013-09-30→10-08 | https://www.shfe.com.cn/publicnotice/notice/201309/t20130910_789836.html | 2 | `reviewed_direct`：两个恢复日均明确写“当晚恢复连续交易” |
| SHFE | holiday_no_night | 《关于2014年休市安排的公告》（〔2013〕20号，2013-12-24）；`E→R`：2013-12-31→2014-01-02、2014-01-30→02-07、04-04→04-08、04-30→05-05、05-30→06-03、09-05→09-09、09-30→10-08 | https://www.shfe.com.cn/publicnotice/notice/201312/t20131224_790109.html | 7 | `reviewed_direct`：年度公告逐项列明停连续交易前夕及照常开市日 |
| SHFE | holiday_no_night | 《关于2015年休市安排的公告》（〔2014〕9号，2014-12-24）；`E→R`：2014-12-31→2015-01-05、2015-02-17→02-25、04-03→04-07、04-30→05-04、06-19→06-23、09-25→09-28、09-30→10-08 | https://www.shfe.com.cn/publicnotice/notice/201412/t20141224_790742.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 《关于中国人民抗日战争暨世界反法西斯战争胜利70周年纪念日期间连续交易时间安排的通知》（上期发〔2015〕121号，2015-08-25）；`E→R`：2015-09-02→09-07 | https://www.shfe.com.cn/publicnotice/notice/201508/t20150825_791123.html | 1 | `reviewed_direct`：明确写“当晚恢复连续交易” |
| SHFE | holiday_no_night | 《关于2016年休市安排的公告》（〔2015〕14号，2015-12-24）；`E→R`：2015-12-31→2016-01-04、2016-02-05→02-15、04-01→04-05、04-29→05-03、06-08→06-13、09-14→09-19、09-30→10-10 | https://www.shfe.com.cn/publicnotice/notice/201512/t20151224_791285.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 《关于2017年休市安排的公告》（〔2016〕141号，2016-12-23）；`E→R`：2016-12-30→2017-01-03、2017-01-26→02-03、03-31→04-05、04-28→05-02、05-26→05-31、09-29→10-09 | https://www.shfe.com.cn/publicnotice/notice/201612/t20161223_792067.html | 6 | `reviewed_direct`：中秋节、国庆节合并为一个前夕 |
| SHFE | holiday_no_night | 《关于2018年休市安排的公告》（〔2017〕88号，2017-12-22）；`E→R`：2017-12-29→2018-01-02、2018-02-14→02-22、04-04→04-09、04-27→05-02、06-15→06-19、09-21→09-25、09-28→10-08 | https://www.shfe.com.cn/publicnotice/notice/201712/t20171222_792839.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 《上海期货交易所关于2019年休市安排的公告》（〔2018〕58号，2018-12-21）；canonical `E→R`：2018-12-28→2019-01-02、2019-02-01→02-11、04-04→04-08、06-06→06-10、09-12→09-16、09-30→10-08；原劳动节恢复条款被调整公告覆盖 | https://www.shfe.com.cn/publicnotice/notice/201812/t20181221_794040.html | 6 | `reviewed_direct_superseded_part`：不从原劳动节条款重复派生 2019-04-30 |
| SHFE | holiday_no_night | 《关于2019年劳动节休市安排的公告》（〔2019〕17号，2019-04-15）；`E→R`：2019-04-30→05-06 | https://www.shfe.com.cn/publicnotice/notice/201904/t20190415_794430.html | 1 | `reviewed_direct_override`：劳动节 canonical 来源 |
| SHFE | holiday_no_night | 《上海期货交易所关于2020年休市安排的公告》（〔2019〕139号，2019-12-24）；`E→R`：2019-12-31→2020-01-02、2020-01-23→01-31、04-03→04-07、04-30→05-06、06-24→06-29、09-30→10-09 | https://www.shfe.com.cn/publicnotice/notice/201912/t20191224_795447.html | 6 | `reviewed_direct`：春节恢复边界后被延长公告修订；04-03、04-30 映射后的 final key 与疫情暂停区间去重 |
| SHFE | holiday_reopening_boundary | 《关于调整2020年春节休市安排的公告》（〔2020〕16号，2020-01-27）；春节休市延长至 02-02，02-03 起照常开市 | https://www.shfe.com.cn/publicnotice/notice/202001/t20200127_795578.html | 0 | `reviewed_boundary`：只闭合修订后的恢复边界，不重述 `notice_evening`；02-03 晚仍受疫情暂停通知约束 |
| SHFE | holiday_no_night | 《上海期货交易所关于2021年休市安排的公告》（〔2020〕213号，2020-12-23）；`E→R`：2020-12-31→2021-01-04、2021-02-10→02-18、04-02→04-06、04-30→05-06、06-11→06-15、09-17→09-22、09-30→10-08 | https://www.shfe.com.cn/publicnotice/notice/202012/t20201223_796877.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 《上海期货交易所关于2022年休市安排的公告》（〔2021〕193号，2021-12-17）；`E→R`：2021-12-31→2022-01-04、2022-01-28→02-07、04-01→04-06、04-29→05-05、06-02→06-06、09-09→09-13、09-30→10-10 | https://www.shfe.com.cn/publicnotice/notice/202112/t20211217_798298.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 《上海期货交易所关于2023年休市安排的公告》（〔2022〕144号，2022-12-27）；`E→R`：2022-12-30→2023-01-03、2023-01-20→01-30、04-04→04-06、04-28→05-04、06-21→06-26、09-28→10-09 | https://www.shfe.com.cn/publicnotice/notice/202212/t20221227_799690.html | 6 | `reviewed_direct`：中秋节、国庆节合并为一个前夕 |
| SHFE | holiday_no_night | 《上海期货交易所关于2024年休市安排的公告》（〔2023〕127号，2023-12-26）；`E→R`：2023-12-29→2024-01-02、2024-02-08→02-19、04-03→04-08、04-30→05-06、06-07→06-11、09-13→09-18、09-30→10-08 | https://www.shfe.com.cn/publicnotice/notice/202312/t20231226_801163.html | 7 | `reviewed_direct` |
| SHFE | holiday_no_night | 2025 年 6 个公告前夕：2024-12-31、2025-01-27、04-03、04-30、05-30、09-30 | https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html | 6 | `reviewed_direct`：公告同时列明恢复日期 |
| SHFE | holiday_no_night | 截止日内 2026 年 3 个公告前夕：2025-12-31、2026-02-13、04-03 | https://www.shfe.com.cn/services/calenderandholidays/holiday/ | 3 | `reviewed_direct` |
| INE | session_launch | SC 于 2018-03-26 上市，夜盘 21:00–02:30 | https://www.ine.cn/publicnotice/notice/201803/t20180312_811177.html | 1 | `reviewed_direct` |
| INE | session_launch | NR 于 2019-08-12 上市，夜盘 21:00–23:00 | https://www.ine.cn/publicnotice/notice/201908/t20190802_811842.html | 1 | `reviewed_direct` |
| INE | session_launch | LU 于 2020-06-22 上市，夜盘 21:00–23:00 | https://www.ine.cn/publicnotice/notice/202006/t20200610_812278.html | 1 | `reviewed_direct` |
| INE | session_launch | BC 于 2020-11-19 上市，夜盘 21:00–01:00 | https://www.ine.cn/publicnotice/notice/202011/t20201111_812502.html | 1 | `reviewed_direct` |
| INE | product_day_only | EC 于 2023-08-18 上市；通知完整枚举 09:00–10:15、10:30–11:30、13:30–15:00 | https://www.ine.cn/publicnotice/notice/202308/t20230811_814262.html | 1 | `reviewed_direct_with_note`：通知未使用“日盘”字样，但完整时段枚举无夜盘 |
| INE | night_suspension | 自 2020-02-03 晚起暂停夜盘，恢复日期另行通知 | https://www.ine.cn/publicnotice/notice/202002/t20200202_812084.html | 0 | `reviewed_boundary`：与恢复边界及全局日历已展开 63 个目标日 |
| INE | night_resumption | 2020-05-06 晚恢复夜盘 | https://www.ine.cn/publicnotice/notice/202004/t20200424_812209.html | 0 | `reviewed_boundary`：闭合暂停区间，不重复计行 |
| INE | holiday_no_night | 《关于2018年休市安排的公告》（〔2018〕6号，2018-03-14）；SC 上市后的 `E→R`：2018-04-04→04-09、04-27→05-02、06-15→06-19、09-21→09-25、09-28→10-08 | https://www.ine.cn/publicnotice/notice/201803/t20180314_811180.html | 5 | `reviewed_direct`：只派生 2018-03-26 夜盘品种上市后的五个前夕 |
| INE | holiday_no_night | 《上海国际能源交易中心关于2019年休市安排的公告》（〔2018〕35号，2018-12-21）；canonical `E→R`：2018-12-28→2019-01-02、2019-02-01→02-11、04-04→04-08、06-06→06-10、09-12→09-16、09-30→10-08；原劳动节恢复条款被调整公告覆盖 | https://www.ine.cn/publicnotice/notice/201812/t20181221_811566.html | 6 | `reviewed_direct_superseded_part`：不从原劳动节条款重复派生 2019-04-30 |
| INE | holiday_no_night | 《关于2019年劳动节休市安排的公告》（〔2019〕6号，2019-04-15）；`E→R`：2019-04-30→05-06 | https://www.ine.cn/publicnotice/notice/201904/t20190415_811704.html | 1 | `reviewed_direct_override`：劳动节 canonical 来源 |
| INE | holiday_no_night | 《上海国际能源交易中心关于2020年休市安排的公告》（〔2019〕30号，2019-12-24）；`E→R`：2019-12-31→2020-01-02、2020-01-23→01-31、04-03→04-07、04-30→05-06、06-24→06-29、09-30→10-09 | https://www.ine.cn/publicnotice/notice/201912/t20191224_812027.html | 6 | `reviewed_direct`：春节恢复边界后被延长公告修订；04-03、04-30 映射后的 final key 与疫情暂停区间去重 |
| INE | holiday_reopening_boundary | 《关于调整2020年春节休市安排的公告》（〔2020〕6号，2020-01-27）；春节休市延长至 02-02，02-03 起照常开市 | https://www.ine.cn/publicnotice/notice/202001/t20200127_812080.html | 0 | `reviewed_boundary`：只闭合修订后的恢复边界，不重述 `notice_evening`；02-03 晚仍受疫情暂停通知约束 |
| INE | holiday_no_night | 《上海国际能源交易中心关于2021年休市安排的公告》（〔2020〕73号，2020-12-23）；`E→R`：2020-12-31→2021-01-04、2021-02-10→02-18、04-02→04-06、04-30→05-06、06-11→06-15、09-17→09-22、09-30→10-08 | https://www.ine.cn/publicnotice/notice/202012/t20201223_812576.html | 7 | `reviewed_direct` |
| INE | holiday_no_night | 《上海国际能源交易中心关于2022年休市安排的公告》（〔2021〕62号，2021-12-17）；`E→R`：2021-12-31→2022-01-04、2022-01-28→02-07、04-01→04-06、04-29→05-05、06-02→06-06、09-09→09-13、09-30→10-10 | https://www.ine.cn/publicnotice/notice/202112/t20211217_813157.html | 7 | `reviewed_direct` |
| INE | holiday_no_night | 《上海国际能源交易中心关于2023年休市安排的公告》（〔2022〕47号，2022-12-27）；`E→R`：2022-12-30→2023-01-03、2023-01-20→01-30、04-04→04-06、04-28→05-04、06-21→06-26、09-28→10-09 | https://www.ine.cn/publicnotice/notice/202212/t20221227_813855.html | 6 | `reviewed_direct`：中秋节、国庆节合并为一个前夕 |
| INE | holiday_no_night | 《上海国际能源交易中心关于2024年休市安排的公告》（〔2023〕74号，2023-12-26）；`E→R`：2023-12-29→2024-01-02、2024-02-08→02-19、04-03→04-08、04-30→05-06、06-07→06-11、09-13→09-18、09-30→10-08 | https://www.ine.cn/publicnotice/notice/202312/t20231226_814527.html | 7 | `reviewed_direct`：官方年度公告覆盖全部七个前夕 |
| INE | holiday_no_night | 2025 年 6 个公告前夕，与 SHFE 2025 年安排一致 | https://www.ine.cn/publicnotice/notice/202412/t20241223_824108.html | 6 | `reviewed_direct` |
| INE | holiday_no_night | 截止日内 2026 年 3 个公告前夕：2025-12-31、2026-02-13、04-03 | https://www.ine.cn/publicnotice/notice/202512/t20251217_829804.html | 3 | `reviewed_direct` |
| INE | holiday_no_night_corroborative | `notice_evening=2024-04-03` | https://www.ine.cn/eng/circularnews/circular/202403/t20240329_823079.html | 0 | `reviewed_direct_corroborative`：已由 2024 年度公告 canonical 覆盖，不重复派生 candidate |
| DCE | session_launch_and_clock_change | P/J 2014-07-04 首批；A/B/M/Y/JM/I 2014-12-26 加入，初始 02:30；2019-03-29 统一至 23:00 并新增 L/V/PP/EG/C/CS | https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf | 22 | `reviewed_direct`：官方回顾材料直接列明产品、日期与时段 |
| DCE | session_clock_change | 自 2015-05-08 21:00（目标交易日 2015-05-11）起，既有 8 品种由 02:30 调整至 23:30 | https://www.dce.com.cn/dalianshangpin/resource/cms/2016/07/%E5%A4%A7%E8%BF%9E%E6%9C%9F%E8%B4%A7%E5%B8%82%E5%9C%BA%E6%9C%88%E6%8A%A5%EF%BC%882015%E5%B9%B45%E6%9C%88%EF%BC%89.pdf | 8 | `reviewed_direct`：与 2019 回顾材料按事件去重，不是重复键 |
| DCE | irregular_night_session | **原文已取回（用户 2026-08-18 提供）**：《关于调整夜盘交易时间的通知》大商所发[2019]553号，2019-12-25 发布。原文「2019年12月25日晚夜盘交易时间调整为22:30—23:00，集合竞价时间为22：25—22：30」，**无品种限定=全所口径**；目标交易日 2019-12-26，该交易日不是 `none` | http://www.dce.com.cn/dalianshangpin/yw/fw/jystz/ywtz/6202113/index.html | 0 | `text_retrieved_pending_url`：正文与文号已坐实，但实际生效 URL 待用户确认（2026-08-18 实测该 URL 与 qhxy 镜像均返回瑞数 JS 挑战 HTTP 412）；候选行见 2026-08-18 待审批次，仍未写入资产 |
| DCE | holiday_reopening_boundary | 2020 年春节休市延长至 02-02，02-03 起照常开市；该通知所称 02-03 当晚恢复夜盘随后被专项暂停通知覆盖 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204380/index.html | 0 | `reviewed_boundary_superseded_part`：只修订 2020 年度公告中的春节恢复边界；不另生成 `notice_evening` |
| DCE | night_suspension | 自 2020-02-03 晚起暂停全部夜盘 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6204446/index.html | 0 | `reviewed_direct`：与恢复边界及全局日历已展开 2020-02-04 至 05-06 的 63 个目标日 |
| DCE | night_resumption | 2020-05-06 晚恢复夜盘，并新增 PG 夜盘 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6215428/index.html | 1 | `reviewed_direct`：1 行只指 PG 启动；闭合暂停区间，不重复计暂停行 |
| DCE | product_day_only | LG 于 2024-11-18 上市，通知明确“暂不开展夜盘交易” | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8620259/index.html | 1 | `reviewed_direct` |
| DCE | holiday_no_night | `E→R`：2014-09-05→09-09 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/1988119/index.html | 1 | `reviewed_direct`：只登记首批夜盘上线后的候选前夕 |
| DCE | holiday_no_night | `E→R`：2014-09-30→10-08 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/1987919/index.html | 1 | `reviewed_direct` |
| DCE | holiday_no_night | 2015 年年度安排；`E→R`：2014-12-31→2015-01-05、2015-02-17→02-25、04-03→04-07、04-30→05-04、06-19→06-23、09-25→09-28、09-30→10-08 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285977/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 抗战胜利 70 周年专项安排；`E→R`：2015-09-02→09-07 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5011746/index.html | 1 | `reviewed_direct` |
| DCE | holiday_no_night | 2016 年年度安排；`E→R`：2015-12-31→2016-01-04、2016-02-05→02-15、04-01→04-05、04-29→05-03、06-08→06-13、09-14→09-19、09-30→10-10 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/5029417/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 2017 年年度安排；`E→R`：2016-12-30→2017-01-03、2017-01-26→02-03、03-31→04-05、04-28→05-02、05-26→05-31、09-29→10-09 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6024178/index.html | 6 | `reviewed_direct`：中秋节、国庆节合并为一个前夕 |
| DCE | holiday_no_night | 2018 年年度安排；`E→R`：2017-12-29→2018-01-02、2018-02-14→02-22、04-04→04-09、04-27→05-02、06-15→06-19、09-21→09-25、09-28→10-08 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6082201/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 2019 年年度安排；canonical `E→R`：2018-12-28→2019-01-02、2019-02-01→02-11、04-04→04-08、06-06→06-10、09-12→09-16、09-30→10-08；原劳动节条款被调整通知覆盖 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6144646/index.html | 6 | `reviewed_direct_superseded_part`：不从原劳动节条款重复派生 2019-04-30 |
| DCE | holiday_no_night | 2019 年劳动节调整；`E→R`：2019-04-30→05-06 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6159945/index.html | 1 | `reviewed_direct_override`：劳动节 canonical 来源 |
| DCE | holiday_no_night | 2020 年年度安排；canonical `E→R`：2019-12-31→2020-01-02、2020-01-23→修订后 02-03、04-03→04-07、04-30→05-06、06-24→06-29、09-30→10-09 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6201818/index.html | 6 | `reviewed_direct_superseded_part`：春节恢复边界由 6204380 修订；04-03、04-30 映射后的 final key 与疫情暂停区间去重 |
| DCE | holiday_no_night | 2021 年年度安排；`E→R`：2020-12-31→2021-01-04、2021-02-10→02-18、04-02→04-06、04-30→05-06、06-11→06-15、09-17→09-22、09-30→10-08 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6260870/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 2022 年年度安排；`E→R`：2021-12-31→2022-01-04、2022-01-28→02-07、04-01→04-06、04-29→05-05、06-02→06-06、09-09→09-13、09-30→10-10 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6300288/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 2023 年年度安排；`E→R`：2022-12-30→2023-01-03、2023-01-20→01-30、04-04→04-06、04-28→05-04、06-21→06-26、09-28→10-09 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8526813/index.html | 6 | `reviewed_direct`：中秋节、国庆节合并为一个前夕 |
| DCE | holiday_no_night | 2024 年年度安排；`E→R`：2023-12-29→2024-01-02、2024-02-08→02-19、04-03→04-08、04-30→05-06、06-07→06-11、09-13→09-18、09-30→10-08 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8589323/index.html | 7 | `reviewed_direct` |
| DCE | holiday_no_night | 2025 年年度安排；`E→R`：2024-12-31→2025-01-02、2025-01-27→02-05、04-03→04-07、04-30→05-06、05-30→06-03、09-30→10-09 | http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html | 6 | `reviewed_direct` |
| DCE | holiday_no_night | `notice_evening=2026-02-13` | http://www.dce.com.cn/dce/content/2026/ywggytz/18627505.html | 1 | `reviewed_direct` |
| DCE | holiday_no_night | `notice_evening=2026-04-03` | http://www.dce.com.cn/dce/content/2026/ywggytz/18628241.html | 1 | `reviewed_direct` |
| CZCE | session_launch | SR、CF、RM、ME/MA、TA 自 2014-12-12 开展夜盘；需与下一条 23:30 时段材料配对 | https://www.czce.com.cn/cn/rootfiles/2014/12/05/1415698821329524-1415698821331547.pdf | 5 | `reviewed_pair_required` |
| CZCE | session_clock | 初始夜盘为周一至周五 21:00–23:30；交易日从前一工作日 21:00 至当日 15:00 | https://www.czce.com.cn/cn/rootfiles/2014/11/27/1415698819273217-1415698819275752.pdf | 0 | `reviewed_direct_policy`：为上一条提供时段，不单独生成重复产品行 |
| CZCE | session_launch | OI、FG、TC/ZC 自 `notice_evening=2015-06-11` 起开展夜盘，目标交易日 2015-06-12，21:00–23:30 | https://www.czce.com.cn/cn/rootfiles/2015/05/27/1431080885614168-1431080885616494.pdf | 3 | `reviewed_direct`：郑商发〔2015〕93号直接列明自然日晚间、生效品种和完整时段；TC 按数据库身份归一为 ZC |
| CZCE | session_clock_snapshot | 2019 年末材料列明相关品种夜盘为 21:00–23:30 | https://www.czce.com.cn/cn/rootfiles/2020/01/13/1572882898738930-1572882898769401.pdf | 0 | `reviewed_direct_corroborative`：只证明材料所述时点的 23:30，不证明后续改为 23:00 的生效日 |
| CZCE | holiday_no_night_rule | 法定节假日后的首个交易日没有前一晚夜盘 | https://www.czce.com.cn/cn/rootfiles/2014/11/24/1415698818159779-1415698818161921.pdf | 0 | `reviewed_rule_only`：没有权威逐年日历时不得批量生成事件行 |
| CZCE | session_launch | CY 自 2017-08-18 晚起，21:00–23:30 | https://www.czce.com.cn/cn/rootfiles/2018/06/29/1505472870185096-1531036138963055.pdf | 1 | `reviewed_direct` |
| CZCE | session_launch | SA 自 2019-12-06 晚起，21:00–23:30 | https://www.czce.com.cn/cn/rootfiles/2019/12/02/1572879248889610-1572879248906593.pdf | 1 | `reviewed_direct` |
| CZCE | session_launch | PF 自 2020-10-12 晚起，21:00–23:00 | https://www.czce.com.cn/cn/rootfiles/2020/10/19/1597089064856174-1597089064867882.pdf | 1 | `reviewed_direct` |
| CZCE | session_launch | SH、PX 自 2023-09-15 晚起，21:00–23:00 | https://www.czce.com.cn/cn/rootfiles/2023/10/18/1697226838884489-1697226838904727.pdf | 2 | `reviewed_direct_batch` |
| CZCE | session_launch | PR 自 2024-08-30 晚起，21:00–23:00 | https://www.czce.com.cn/cn/rootfiles/2024/09/14/1726389999932285-1726389999951648.pdf | 1 | `reviewed_direct_batch` |
| CZCE | holiday_no_night | `notice_evening=2018-12-28`；2019-01-02 晚恢复夜盘 | https://www.czce.com.cn/cn/rootfiles/2018/12/24/1545632831296256-1545632831311552.pdf | 1 | `reviewed_direct`：只保存公告前夕，目标日由全局日历计算 |
| CZCE | night_suspension | 自 2020-02-03 晚起暂停夜盘；首个疫情暂停目标日为 2020-02-04 | http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/02/1572884629536892.htm | 0 | `official_url_manual_fetch`：官方旧页当前 412；与恢复边界及全局日历合用派生 63 行，2020-02-03 另按节后规则归因 |
| CZCE | night_resumption | 2020-05-06 晚恢复商品期货、期权夜盘；首个恢复夜盘目标日为 2020-05-07 | http://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2020/04/1584386593065063.htm | 0 | `official_url_manual_fetch`：郑商函〔2020〕153号；闭合暂停区间，不重复计行 |
| GFEX | session_launch_schedule | SI 于 2022-12-22 上市；通知列明三个日盘小节，同时保留“交易所规定的其他时间” | https://www.gfex.com.cn/gfex/tzts/202212/44ccfcb613e442658c8ac94861e0de18.shtml | 0 | `reviewed_direct_launch_but_blocked_history`：证明上市日与日盘安排，不足以授权连续 day-only 区间 |
| GFEX | session_launch_schedule | LC 于 2023-07-21 上市；通知列明三个日盘小节，同时保留“交易所规定的其他时间” | https://www.gfex.com.cn/gfex/tzts/202307/33f2a342d80f4ee69966df4a554c26a4.shtml | 0 | `reviewed_direct_launch_but_blocked_history`：同上 |
| GFEX | session_launch_schedule | PS 于 2024-12-26 上市；通知列明三个日盘小节，同时保留“交易所规定的其他时间” | https://www.gfex.com.cn/gfex/tzts/202412/34bc2f9dbfc34b4b81e1a043ff526589.shtml | 0 | `reviewed_direct_launch_but_blocked_history`：同上 |
| GFEX | session_launch_schedule | PT 于 2025-11-27 上市；通知列明三个日盘小节，同时保留“交易所规定的其他时间” | https://www.gfex.com.cn/gfex/tzts/202508/4d8af56888c84490b525d5d8fdd729f6.shtml | 0 | `reviewed_direct_launch_but_blocked_history`：页面发文日为 2025-11-14，不得按 URL 的 202508 目录推断日期 |
| GFEX | session_launch_schedule | PD 于 2025-11-27 上市；通知列明三个日盘小节，同时保留“交易所规定的其他时间” | https://www.gfex.com.cn/gfex/tzts/202508/5ff7c8717a4a44708e650a08b198254f.shtml | 0 | `reviewed_direct_launch_but_blocked_history`：页面发文日为 2025-11-14，不得按 URL 的 202508 目录推断日期 |
| GFEX | current_day_schedule | SI 合约列明日盘三节；库内首日为 2022-12-22 | https://www.gfex.com.cn/gfex/llbb/202402/73cd6f4cc26b4dd5b3d127b5462b59a7/files/%E5%B9%BF%E5%B7%9E%E6%9C%9F%E8%B4%A7%E4%BA%A4%E6%98%93%E6%89%80%E5%B7%A5%E4%B8%9A%E7%A1%85%E6%9C%9F%E8%B4%A7%E5%90%88%E7%BA%A6%EF%BC%882022%E5%B9%B412%E6%9C%8812%E6%97%A5%E7%89%88%EF%BC%89.pdf | 0 | `review_blocked_history`：“及交易所规定的其他时间”不能证明自上市以来持续日盘 |
| GFEX | current_day_schedule | LC 合约列明日盘三节；库内首日为 2023-07-21 | https://www.gfex.com.cn/gfex/sytslqhhy/202307/9ad927b8ec4c458594e172fd1ada2a9b.shtml | 0 | `review_blocked_history`：同上 |
| GFEX | current_day_schedule | 当前产品页列明 PS、PT、PD 日盘时段；库内首日分别为 2024-12-26、2025-11-27、2025-11-27 | https://www.gfex.com.cn/gfex/sspzb/sspz.shtml | 0 | `review_blocked_history`：仍缺连续制度证据 |

## 数量与运行前门槛

只读 SQL 对 `public.futures_daily` 在请求截止日以前的商品品种盘点为：CZCE 27、
DCE 23、GFEX 5、INE 5、SHFE 20，共 **80 个产品身份**。这是制度覆盖工作清单，
不是最终 `day_only` 行数；初始人工排期按最多 80 个产品区间检查，实际非重叠区间数
必须由 round 1 inventory 与上市/变更公告共同确定。

统计于 2026-08-14 使用生产只读连接、`use_test=False` 和截止日 2026-04-29 执行：

```sql
WITH product_history AS (
    SELECT
        split_part(symbol, '.', 2) AS exchange_suffix,
        upper(substring(symbol from '^[A-Za-z]+')) AS product,
        min(trade_date) AS first_trade_date,
        max(trade_date) AS last_trade_date,
        count(DISTINCT symbol) AS contracts
    FROM public.futures_daily
    WHERE trade_date <= DATE '2026-04-29'
      AND COALESCE(
          NOT (
              upper(substring(symbol from '^[A-Za-z]+')) = ANY(
                  ARRAY['IF', 'IC', 'IH', 'IM', 'T', 'TF', 'TL', 'TS']::text[]
              )
          ),
          TRUE
      )
    GROUP BY 1, 2
    HAVING max(trade_date) >= DATE '2011-01-01'
)
SELECT exchange_suffix, product, first_trade_date, last_trade_date, contracts
FROM product_history
ORDER BY exchange_suffix, product;
```

确定性产品身份摘要如下；历史材料中的 ME/TC 是 MA/ZC 的旧代码，不另增数据库身份：

| daily_suffix | count | products |
|---|---:|---|
| CZC | 27 | AP, CF, CJ, CY, FG, JR, LR, MA, OI, PF, PK, PL, PM, PR, PX, RI, RM, RS, SA, SF, SH, SM, SR, TA, UR, WH, ZC |
| DCE | 23 | A, B, BB, BZ, C, CS, EB, EG, FB, I, J, JD, JM, L, LG, LH, M, P, PG, PP, RR, V, Y |
| GFE | 5 | LC, PD, PS, PT, SI |
| INE | 5 | BC, EC, LU, NR, SC |
| SHF | 20 | AD, AG, AL, AO, AU, BR, BU, CU, FU, HC, NI, OP, PB, RB, RU, SN, SP, SS, WR, ZN |

| counter | current_value | publication_gate |
|---|---:|---|
| official source-register entries | 96 | 只计上表官方域名 URL；同 URL 的不同事实仍只算一个来源；包含 1 条待人工取回正文的 DCE 官方旧页 |
| accepted exact `none,none` evening candidates | 219 | 上轮 143 加 DCE 2014–2025 的 76 个 canonical 候选；必须逐行映射到下一全局 `trade_date` 后才能写 session exception CSV |
| DCE `none,none` working candidates | 78 | DCE 2014–2025 的 76 个唯一 `notice_evening`，加现有 2026-02-13、04-03 两项；不含尚待 inventory 与官方来源闭合的 2025-12-31 |
| known irregular-session candidates | 1 | DCE 目标交易日 2019-12-26；不是 `none`，不计入 219 或 DCE 78。承载它的 schema 已于 2026-08-18 实施（见下），但本次实施**没有**提交任何该日的 exception 行；仍待 `6202113` 原文取回并复核 |
| holiday `none,none` rows working estimate | 300–450 | 只估四所节前日期，不含 2020 暂停期；以 inventory 为准 |
| 2020 suspension expanded rows | 252 | SHFE、INE、DCE、CZCE 各 63 个目标日（2020-02-04 至 2020-05-06）；不含另按节后规则归因的 2020-02-03 |
| expected total `none,none` exception rows | not measured | 节前日期与 2020 展开去重后的并集 |
| products requiring regime classification | 80 | 每个实际 audit 产品必须有经验时段或权威 day-only 解释 |
| accepted 2020 suspension/resumption boundaries | 8 | SHFE、INE、DCE、CZCE 各一停一复；CZCE 旧页当前按 `official_url_manual_fetch` 登记 |
| unresolved empirical dates | not measured | round 1 必须一次性输出全量，禁止逐条挤牙膏 |
| exact source-candidate duplicate keys | 0 | 当前已登记精确候选按来源键去重；非零禁止进入 capture |
| expanded final-key duplicates | not measured | 日历展开后必须为 0，否则禁止写 authority CSV |

`2020 suspension expanded rows` 于 2026-08-14 使用生产只读连接执行以下 SQL；
四个日线后缀的结果均为 63 个目标交易日，合计 252：

```sql
SELECT
    split_part(symbol, '.', 2) AS daily_suffix,
    count(DISTINCT trade_date) AS target_days,
    min(trade_date) AS first_target,
    max(trade_date) AS last_target
FROM public.futures_daily
WHERE trade_date BETWEEN DATE '2020-02-04' AND DATE '2020-05-06'
  AND split_part(symbol, '.', 2) = ANY(
      ARRAY['SHF', 'INE', 'DCE', 'CZC']::text[]
  )
GROUP BY 1
ORDER BY 1;
```

结果为 `CZC 63`、`DCE 63`、`INE 63`、`SHF 63`，每所首尾目标日均为
`2020-02-04` 与 `2020-05-06`。

`official source-register entries` 按唯一 URL 去重计数；当前恰为 96，与来源表
行数相同。

本轮历史节前来源 canonical 集合共 122 个 `(exchange, notice_evening)`：SHFE 77、
INE 45。现有 INE `notice_evening=2024-04-03` 英文 circular 已改为只作佐证，故
相对原登记净新增 121 个候选，上轮 accepted 总数为 `22 - 1 + 122 = 143`。
DCE 2014–2025 的 15 个节前来源 URL 共给出 76 个唯一 `notice_evening`，其中
2019 年劳动节只以调整通知为 canonical 来源；加上已登记的 2026-02-13、04-03，
DCE 工作清单为 78 项，accepted 总数为 `143 + 76 = 219`。2025-12-31 晚间对应
2026 年元旦安排，虽可能映射到本次截止日内的目标交易日，但当前不计入 78，也不在
inventory 与官方来源完成闭合前写 CSV。2020 年 `notice_evening=04-03`、`04-30`
在 SHFE、INE、DCE 各有一项，映射后共 6 个 final key 会与疫情暂停区间去重；
来源层仍保留公告事实，但不得写成第二个 CSV 键。DCE 的 2020-01-23 候选按延长
通知复核恢复边界为 02-03；暂停区间从 02-04 目标交易日起算，因此两者不重叠。
DCE 2019-12-25 晚 22:30–23:00 的异常时段候选只增加来源登记项，不是停夜盘，
因此不计入 219 个 exact `none,none` 候选或 DCE 78 项工作清单。

在首次全量 capture 前，上述“未测”允许存在；一旦 round 1 产生 inventory，必须替换为
确定数字，并记录每轮剩余量。预计总共运行 2–3 轮：第一轮完整盘点，第二轮批量补表，
第三轮只用于验证批量修订是否收敛；第三轮仍非零则停止发布。

## 未解决项

- SHFE 2013 年 CU/AL/ZN/PB 与 2014 年 RB/HC/RU/BU 的原始操作通知尚未恢复；
  现有交易所材料只能标为佐证；此项只涉及品种夜盘启动制度，不再是节前来源缺口。
- SHFE 自 2013-07 夜盘上线后至 2024 年休市安排、INE 自 SC 2018-03-26 上市后至
  2024 年休市安排的节前来源均已完整覆盖。INE 2024 年官方年度公告明确覆盖 7 个
  前夕：2023-12-29、2024-02-08、04-03、04-30、06-07、09-13、09-30。
- DCE 2014–2025 的 76 个唯一候选公告前夕已由 15 个可直读的交易所官方 URL
  覆盖，2019 年劳动节 override 与 2020 年三条边界均已闭合；连同现有
  2026-02-13、2026-04-03 两项，当前工作清单为 78。2025-12-31 晚间尚待
  inventory、下一全局目标交易日映射与对应官方来源共同复核，不计入 78，也不得
  临时写入 CSV。
- DCE 官方旧页 `6202113` 所对应的异常时段候选为 2019-12-25 晚
  22:30–23:00（目标交易日 2019-12-26）。**2026-08-18 由用户人工取回正文**：
  《关于调整夜盘交易时间的通知》大商所发[2019]553号，原文确认 22:30—23:00
  且无品种限定（全所口径），另给出集合竞价 22:25—22:30。登记状态改为
  `text_retrieved_pending_url`（仅缺实际生效 URL），`rows_derived` 仍维持 0。该日有夜盘，
  不是 `none`。承载它所需的 schema 已于 2026-08-18 实施：会话资产与 exception
  资产都已能表达 `night_start=22:30`（见下节）。**但本次实施没有提交任何
  DCE 2019-12-26 的 exception 行**——schema 就绪不等于权威就绪。发布覆盖该日的
  分钟时段资产前仍必须先取回原文，不能把它降级为 `none` 或普通 21:00–23:00 时段。
- CZCE 的 OI/FG/TC(ZC) 2015 夜盘启动已由郑商发〔2015〕93号闭合；2019-12 的
  23:30→23:00 变更已定位为郑商函〔2019〕473号，签发日 2019-12-10、自然日晚间
  2019-12-11 生效、目标交易日 2019-12-12，但仍缺可登记的 CZCE 原页或附件 URL；
  绝大多数逐年节前通知也仍缺直接官方 URL，2020 暂停/恢复边界已恢复官方旧 URL。
- GFEX 的 SI、LC、PS、PT、PD 上市通知与当前页面只证明上市日和所列日盘结构，
  尚不足以授权从上市日至 2026-04-29 的连续 day-only 区间。
- WR、部分 CZCE 日盘品种以及代码迁移 ME/MA、TC/ZC 需要历史规则与别名复核。
- 2020 暂停区间已由“暂停边界 + 恢复边界 + 全局交易日历”展开为 252 个来源候选；
  2020-02-03 必须另按节后首交易日归因，与区间内节假日公告重叠时仍只保留一个键。

这些缺口不会用第三方镜像、经验缺行、普通周末或当前规则反向外推填补。Task 7
只有在官方来源、目标日期映射和经验 inventory 三者一致后才写权威 CSV。

## 2026-08-18 session exception schema 实施状态

权威资产已从"只能表达无夜盘"泛化为"按交易所/目标交易日的精确夜盘区间例外"。

- 资产更名：`config/carry_minute_no_night_dates.csv` →
  `config/carry_minute_session_exceptions.csv`，表头为
  `exchange,version,trade_date,night_start,night_end,reason,source_url`。
- 哈希清单键 `no_night` → `session_exception`；采集日志行相应改为
  `session_exception_sha256=`。
- 会话资产 `config/carry_minute_sessions.csv` 的表头增加 `night_start`，
  完整表头为
  `exchange,product,effective_start,effective_end,night_start,night_end,version`。

**当前仓库资产仍是纯表头，零数据行。** 本次实施只交付 schema 与校验代码，
没有写入任何 exception 行。上表所有 `reviewed_*` 的节前休市来源，其地位不变：
它们是**将来某个已复核批次**中可用于派生 `none,none` exception 行的合格来源，
本次没有派生、也没有提交其中任何一行。DCE `6202113` 同样保持 0 行。

实施证据见 `docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`。

## 去重与复核规则

- session exception 唯一键：`(version, exchange, trade_date)`；研究阶段先以
  `(exchange, notice_evening)` 去重，再由全局日历映射目标日。
- 制度区间唯一键：`(version, exchange, product, effective_start)`；同产品区间不得重叠。
- DCE 2019 回顾材料会复述 2014/2015 事件；按事件选择一个 canonical 来源，不生成重复行。
- DCE 2019 年度劳动节条款由单独调整通知覆盖；2020 年 04-03、04-30 的节前候选
  映射后与疫情暂停目标日相交，只保留一个最终 authority 键。
- CZCE 单项通知与月度公告汇编重复时优先单项通知；ME/MA、TC/ZC 先做别名归一。
- SHFE 与 INE 分交易所记录；INE 通知即使由 SHFE 官方站镜像，也只产生 INE 行。
- 2020 暂停范围内的普通节前停夜盘公告可保留来源记录，但不能产生第二个 CSV 键。
- 当前交易时段页只用于截止日反向核对，不得覆盖有日期的历史通知。
