# Phase 1 embedding A/B eval (PLAN.md Phase 1 item 6):
# 20 Chinese finance questions, multilingual MiniLM vs bge-small-zh.
# Retrieval task: each query must hit its known-relevant chunk in a pool
# with distractors. Winner becomes DEFAULT_EMBEDDING_MODEL / meta lock.
# Run: .venv/Scripts/python -X utf8 scripts/ab_eval.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.config import EMBEDDING_CANDIDATES
from app.rag.embedder import Embedder

# (query, relevant doc) — docs are written like transcript chunks with a
# title prefix, queries phrased colloquially to test paraphrase robustness.
PAIRS = [
    ("台積電第四季毛利率多少", "台積電法說會\n公司公布第四季毛利率達到 53%,高於市場預期,主要受惠於三奈米製程放量。"),
    ("2330 股價還會漲嗎", "台股分析\n2330 台積電目前本益比約 25 倍,外資持續買超,技術面呈現多頭排列。"),
    ("0050 跟 006208 差在哪", "ETF 比較\n元大台灣50(0050)與富邦台50(006208)追蹤同一指數,主要差異在經理費與規模。"),
    ("聯準會什麼時候降息", "美國貨幣政策\n聯準會主席表示通膨回落至目標前不會急於降息,點陣圖顯示今年預計降息兩次。"),
    ("EPS 是什麼意思", "財報入門\n每股盈餘(EPS)是公司稅後淨利除以流通在外股數,反映每一股賺多少錢。"),
    ("庫藏股對股價有什麼影響", "公司治理\n公司實施庫藏股買回自家股票,減少流通籌碼,通常被解讀為護盤或對股價有信心。"),
    ("輝達財報開得怎麼樣", "NVIDIA 財報\n輝達資料中心營收年增超過四倍,執行長黃仁勳表示 AI 需求仍供不應求。"),
    ("現在該買美債嗎", "債券投資\n美國十年期公債殖利率站上 4.5%,存續期間較長的債券 ETF 對利率變動更敏感。"),
    ("通膨數據 CPI 公布結果", "總經數據\n最新消費者物價指數(CPI)年增率 2.9%,核心 CPI 仍高於聯準會 2% 目標。"),
    ("高股息 ETF 配息會不會縮水", "高股息投資\n00878 等高股息 ETF 的配息來自成分股股利與資本利得平準金,殖利率並非保證。"),
    ("融資斷頭是什麼", "信用交易\n融資維持率低於 130% 會收到追繳通知,未補繳將被券商強制賣出,俗稱斷頭。"),
    ("日圓還會再貶嗎", "外匯市場\n日本央行維持超寬鬆政策,美日利差擴大使日圓兌美元貶破 155 關卡。"),
    ("除權息是什麼時候", "股利行事曆\n上市公司除權息旺季集中在六到九月,參加除息需在除息日前一天持有股票。"),
    ("聯發科跟高通誰比較強", "半導體競爭\n聯發科天璣旗艦晶片在安卓高階市場市占率提升,與高通驍龍正面競爭。"),
    ("房貸利率會不會升", "房市金融\n央行升息半碼後,五大銀行新承做房貸利率突破 2.2%,創十五年新高。"),
    ("什麼是本益比", "估值指標\n本益比(P/E)是股價除以每股盈餘,衡量市場願意為每一元獲利付出多少價格。"),
    ("比特幣 ETF 通過了嗎", "加密資產\n美國證管會核准現貨比特幣 ETF 上市,傳統資金可透過券商帳戶參與。"),
    ("鴻海電動車進度", "鴻海轉型\n鴻海 MIH 平台電動車已交付北美客戶,董事長劉揚偉目標 2025 年市占 5%。"),
    ("升息對科技股影響", "利率與估值\n升息環境下高成長科技股估值承壓,因為未來現金流折現率上升。"),
    ("KD 指標黃金交叉怎麼看", "技術分析\nKD 隨機指標中 K 值由下往上穿越 D 值稱為黃金交叉,常被視為短線買進訊號。"),
]

DISTRACTORS = [
    "旅遊 vlog\n今天帶大家去日本東京自由行,淺草寺跟晴空塔一日遊路線分享。",
    "健身教學\n深蹲時膝蓋不要內夾,核心收緊,重量循序漸進才不容易受傷。",
    "料理頻道\n今天教大家做紅燒牛肉麵,牛腱要先汆燙去血水,燉兩小時最軟嫩。",
    "3C 開箱\n這支手機的相機在夜拍表現出色,螢幕更新率 120Hz 滑起來很順。",
    "電影影評\n這部片的敘事結構採非線性剪輯,配樂跟攝影都是奧斯卡等級。",
    "露營分享\n這個營地海拔一千公尺,夏天晚上只有 15 度,記得帶保暖衣物。",
    "語言學習\n背單字最有效的方法是間隔重複,搭配例句記憶效果更好。",
    "寵物日常\n貓咪一直舔毛可能是焦慮或皮膚問題,建議帶去獸醫檢查。",
    "園藝教學\n多肉植物澆水要乾透再澆,夏天休眠期更要控水避免爛根。",
    "親子教育\n小孩寫作業拖拖拉拉,可以用番茄鐘把時間切成小段落。",
]


def evaluate(model_name: str) -> dict:
    emb = Embedder(model_name)
    docs = [doc for _, doc in PAIRS] + DISTRACTORS
    doc_vecs = np.stack(emb.embed_texts(docs))
    doc_vecs = doc_vecs / np.linalg.norm(doc_vecs, axis=1, keepdims=True)
    hit1 = hit3 = 0
    misses = []
    for i, (query, _) in enumerate(PAIRS):
        q = emb.embed_query(query)
        q = q / np.linalg.norm(q)
        ranking = np.argsort(-(doc_vecs @ q))
        if ranking[0] == i:
            hit1 += 1
        if i in ranking[:3]:
            hit3 += 1
        else:
            misses.append(query)
    return {"model": model_name, "hit@1": hit1, "hit@3": hit3, "misses": misses}


def main() -> None:
    results = [evaluate(name) for name in EMBEDDING_CANDIDATES.values()]
    print(f"\n{'model':<60} hit@1  hit@3  (n={len(PAIRS)})")
    for r in results:
        print(f"{r['model']:<60} {r['hit@1']:>5}  {r['hit@3']:>5}")
        if r["misses"]:
            print(f"  top-3 misses: {r['misses']}")
    # Winner: hit@1 first, hit@3 tiebreak; stable order favors first candidate.
    winner = max(results, key=lambda r: (r["hit@1"], r["hit@3"]))
    print(f"\nWINNER: {winner['model']}")
    print("→ 請將 app/config.py 的 DEFAULT_EMBEDDING_MODEL 設為勝者;"
          "首次攝取時會鎖入 meta 表。")


if __name__ == "__main__":
    main()
