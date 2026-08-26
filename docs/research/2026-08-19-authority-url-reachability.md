# 权威来源 URL 的机读可达性实测（2026-08-19 下午）

## 结论先行

**「URL 我能不能打开」不能作为权威来源的取舍标准。** 本机实测五个交易所域名，
只有 CZCE 的 PDF 直链能取到正文；其余四所（SHFE / DCE / INE / GFEX）今天全部
不可机读。而批次 D 里已被当作「已核实」的 SHFE / DCE / INE 行，用的正是这些
今天取不到的 URL —— 同一把尺子会把它们一起否掉。

因此 `docs/research/2026-08-19-carry-minute-pending-batch-cd.md` 缺陷 3
（「SHFE.NI 2022-03-10 无经核实来源，因为 URL 是 404」）**前提有误**，见下。

## 🔑 2026-08-19 傍晚追加：`shfe.cn` 可机读，`shfe.com.cn` 不可

用户提供公告 URL 时用的是 **`shfe.cn`** 域名，实测**两个域名行为完全不同**：

| 域名 | 结果 |
|---|---|
| `www.shfe.com.cn/...` | ⚠️ 200 + 瑞数 WAF 校验页，取不到正文 |
| `www.shfe.cn/...` | ✅ **200 + 完整正文**，同一路径同一份公告 |

已实测两份：上期发〔2022〕73 号（镍暂停）与〔2025〕157 号（2026 年休市安排），
均取到全文。**路径部分完全一致，只需把域名换掉。**

这条把上期所从「我读不到，只能你在浏览器核」变成「我能自己核」——
登记表里所有 `shfe.com.cn` 的来源，现在都可以换域名复核正文。
下表的 SHFE 行记录的是 `shfe.com.cn` 的行为，仍然属实，但不再意味着无法核实。

## 实测结果

方法：`curl -sL --max-time 30 -A "<Chrome UA>"`，从本开发机直连。

| 域名 | 样本 URL | 结果 |
|---|---|---|
| `www.czce.com.cn` | `/cn/rootfiles/2014/11/24/...pdf` | ✅ 200，177 KB `application/pdf`，正文可取 |
| `www.czce.com.cn` | `/cn/rootfiles/2014/12/05/...pdf` | ✅ 200，219 KB `application/pdf` |
| `www.shfe.com.cn` | `/news/notice/911341410.html`（镍暂停通知） | ⚠️ **200**，返回瑞数 WAF 人机校验页 |
| `www.shfe.com.cn` | `/publicnotice/notice/202412/t20241223_824109.html` | ⚠️ 200 + 同一 WAF 页 |
| `www.shfe.com.cn` | `/publicnotice/notice/202212/t20221227_799690.html` | ⚠️ 200 + 同一 WAF 页 |
| `www.shfe.com.cn` | 站点首页 `/` | ⚠️ 200 + 同一 WAF 页 |
| `www.ine.cn` | `/publicnotice/notice/202412/t20241223_824108.html` | ⚠️ 200 + WAF 页 |
| `qhxy.dce.com.cn` | `/dalianshangpin/ywfw/jystz/ywtz/8626934/index.html` | ❌ 412 JS 挑战 |
| `www.dce.com.cn` | `/dalianshangpin/resource/cms/2019/04/...pdf` | ❌ 连接挂死（60 s 无响应） |
| `www.gfex.com.cn` | `/gfex/tzts/202212/...shtml` | ❌ 连接失败 |

换浏览器 UA、加 `Referer`、加 `Accept-Language` 都不改变 SHFE 的 WAF 结果。

## 对缺陷 3 的修正

原记「`https://www.shfe.com.cn/news/notice/911341410.html` 实测 HTTP 404，不可采用」。
今日复测该 URL 返回 **200**，内容是 WAF 校验页 —— 与上期所**首页以及批次 D 中已采纳的
两条 SHFE 公告 URL 完全同一表现**。所以：

- 这条 URL 没有证据表明它是死链；
- 「上期所站点本身可机读（首页 200）」这句原始判断也需修正：首页的 200 同样是 WAF 页，
  不是站点内容。当时把 HTTP 状态码当成了可读性。

**真正的缺口不变、但性质变了**：缺的不是「一个能打开的 URL」，而是**该公告正文的存证**
（哪些合约、暂停哪一天、是否含夜盘）。这与 CZCE 清单里的做法一致 ——
由用户在浏览器打开并贴回链接/正文，脚本负责取证与生成。

## 由此确立的取舍标准

1. `source_url` 只需是**该公告的官方规范路径**；代码层也只校验非空文本
   （`cta_carry/session_authority.py` 对 `source_url` 仅做 `_validate_required_record_text`），
   从不校验可达性。
2. 判断一行能否登记，看的是**正文是否经核验**（我取到，或用户在浏览器核对后确认），
   不是我这台机器今天能否连上。
3. 记录取证时的实际观测（状态码 + 是否 WAF 页），别把状态码当正文。

相关：`docs/research/2026-08-19-czce-holiday-notice-shopping-list.md`（同一约束下
CZCE 的处理方式）、`docs/research/2026-08-19-carry-minute-pending-batch-cd.md`。
