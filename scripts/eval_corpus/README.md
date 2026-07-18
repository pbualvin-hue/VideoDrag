# 檢索評測(retrieval eval)

離線、零 API 費的檢索品質量測(EVAL-AND-OPENSOURCE-PLAN 缺口 1)。用固定的
fixture 語料 + golden 問答集,量測改動 chunking／embedding 模型／檢索合併邏輯
後,recall 是升還是降,並產出「向量 vs FTS5 誰救了誰」的證據表。

## 怎麼跑

```
.venv/Scripts/python scripts/eval_retrieval.py           # recall@5(預設)
.venv/Scripts/python scripts/eval_retrieval.py --k 3
```

- 完全離線:用本機 fastembed(首次會下載模型),不需任何 API key、零費用。
- 每次執行把一組指標 append 到 `eval_history.jsonl`(日期、參數、分數)。
- 也可經 `RUN_EVAL=1 .venv/Scripts/python scripts/run_all_tests.py` 併入回歸套件。

## 構成

- `docs/*.txt` — 10 篇固定逐字稿 fixture,涵蓋財經／健身／科技,含刻意設計的
  代號與專有名詞(0050、2330、TLT、CoWoS、Zone 2、168…)供精確匹配測試。
  **皆為示範用的通用衛教內容,非投資建議、非真實個人資料。**
- `golden.jsonl` — 每題:`query`、`category`、`expect`(應命中的來源 + 段落
  子字串)。類別涵蓋既有已知弱點:`ticker_exact`(FTS 強項)、`paraphrase`
  (向量強項)、`proper_noun`、`cross_source`(應命中多來源)。
- `eval_retrieval.py` — 用**真實** ingest 管線灌進臨時 DB(順便回歸攝取層)、
  跑**真實**混合檢索 `app.rag.chat.retrieve`,算 recall@k、MRR、路由歸屬。
  載入時會驗證每個 gold 子字串確實出現在其來源(fixture 打錯字直接報錯)。

## 指標

- **recall@k**:top-k 內是否命中任一應命中段落(命中率)。
- **MRR**:第一個命中的名次倒數平均。
- **coverage**:cross_source 題平均命中幾成的應命中段落。
- **route attribution**:命中的 gold chunk 是被「向量獨力/FTS 獨力/兩路皆有」
  找到——這張表是混合檢索設計價值的直接證據。

## 基準與首次調校(2026-07-18)

`BAAI/bge-small-zh-v1.5`、RRF_K=60、max_vector_distance=1.0、k=5:

| 版本 | recall@5 | MRR | proper_noun | 負面 false-hit(共 4) |
|---|---|---|---|---|
| 初版 | 0.97 | 0.95 | 0.86 | 1 |
| **`_fts_query` 修正後** | **1.00** | **0.98** | **1.00** | 1(未變) |

**harness 首跑即抓到一個檢索問題並導出正確修法**:唯一的 miss
(q10「乳酸閾值是什麼?」)不是排序錯——正確來源就是向量最近鄰(距離 1.0707),
但全部向量距離都略高於 `MAX_VECTOR_DISTANCE=1.0` 門檻被整批濾掉,同時 FTS 把整段
中文 query 當成單一 phrase 而未命中,`retrieve` 回傳空。

- **放寬門檻是錯的**:門檻 sweep(1.0/1.15/1.3)顯示,能救 q10 的門檻(需 ≥1.07)
  也讓**全部 4 個負面 query 都回傳雜訊**(false-hit 1→4)——bge-small-zh 的
  L2 距離對短 query 分離度太差,沒有一個門檻能同時保 recall 又擋雜訊。放寬會讓
  對話引用到不相關來源(違反 rule 13-1 誠實歸屬),故**門檻維持 1.0**。
- **正解是修 FTS 斷詞**:`_fts_query` 先剝除常見疑問/填充詞(是什麼/怎麼/如何…),
  長中文 query 就不會塌成一個無法命中的 phrase term,內容詞(乳酸閾值)才真的去查。
  proper_noun recall 0.86→1.00、**零新增 false-hit**。這是 harness 導出的
  「先量測、放寬被否決、找到零成本正解」完整循環。
- 殘留:1 個負面(n02 高鐵→fin_0050)在門檻 1.0 下由向量路徑誤召,與本次 FTS
  修正無關;要不要收斂屬另一次(收緊會傷 recall)的實驗,留 backlog。

## 回答品質評測(gap 2,`eval_answers.py`)

LLM-as-judge:把 `qa_golden.jsonl` 的問題跑過**完整 RAG 鏈**(真實 `chat.answer`),
再用 Haiku 依 CLAUDE.md rule 13 / 13-1 逐條評分——faithfulness(只據來源)、citation
(出處含標題+日期)、attribution(「作者主張/影片提到」歸屬);「應拒答」題直接檢查
未杜撰。**會花 API 費**(每題=1 回答+1 judge),golden 集刻意小。

```
python scripts/eval_answers.py            # 真實 config(Sonnet 回答)
python scripts/eval_answers.py --budget    # Haiku 回答,省錢
python scripts/eval_answers.py --limit 4
```

**首基準(2026-07-18,`--budget` Haiku 回答+judge,7 題+1 拒答)**:
attribution **7/7**、faithful **5/7**、citation **0/7**、拒答 **1/1**。

- **attribution 100%** — SYSTEM_PROMPT 的歸屬規則穩定生效。
- **faithful 71%** — budget/Haiku 回答偶爾漂移(a03/a06);換 accurate(Sonnet)預期更高,
  待補一次 accurate 基準。
- **citation 0% 是 fixture 假象**:本語料全是 text 型來源(無時間戳、日期=入庫日),
  judge 正確地判「出處三元組不完整」。**要有意義地量 citation,需加一支 video 型
  fixture(帶真實時間戳+發布日)** — 列 backlog。
- 拒答題(比特幣該不該買)正確回「庫內沒有相關內容」(rule 1)。

## 擴充方向

- 加長部分 fixture 讓單篇切成多 chunk,測「同篇內段落級」檢索(目前 10 篇≈10 chunk)。
- 擴到 50–100 題;把真實庫內容匯出補充(注意去識別化)。
- 缺口 2(回答品質 LLM-as-judge)與缺口 3(檢索 tracing)見 EVAL-AND-OPENSOURCE-PLAN.md。
