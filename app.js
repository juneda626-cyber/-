let companies = [];
let activeFilter = "all";
const urgencyLabel = { high: "緊急度 高", mid: "緊急度 中", low: "緊急度 低", undetermined: "判定不可" };
const colors = ["#5b8fa0", "#796ba8", "#4f9675", "#4b79a5"];

const activities = [
  { type:"buy", icon:"⇄", tag:"M&A", time:"32分前", title:"株式会社アルファリンク", text:"成長戦略資料にて、DX領域の買収方針を明示" },
  { type:"new", icon:"↗", tag:"新規事業", time:"1時間前", title:"東洋マテリアル株式会社", text:"蓄電池リサイクル事業への参入を発表" },
  { type:"invest", icon:"◇", tag:"投資", time:"3時間前", title:"ホライズンHD株式会社", text:"CVC新設、投資枠30億円を設定" },
  { type:"buy", icon:"◎", tag:"決算資料", time:"昨日", title:"株式会社デジタルフロンティア", text:"非連続成長に向けた投資方針をアップデート" }
];
const matches = [
  { a:"TM", ac:"#527b94", b:"LP", bc:"#4a9b7a", score:92, title:"製造DX × AI画像解析", tags:["生産性向上","全国展開"] },
  { a:"RH", ac:"#8069a5", b:"EC", bc:"#53928b", score:88, title:"物流網 × ラストワンマイル", tags:["物流2026","顧客基盤"] },
  { a:"MD", ac:"#447ba0", b:"HB", bc:"#c18365", score:84, title:"医療データ × 予防ヘルスケア", tags:["データ活用","ストック型"] }
];

const companyList = document.querySelector("#companyList");
function renderCompanies() {
  const q = document.querySelector("#globalSearch").value.trim().toLowerCase();
  const visible = companies.filter(c => (activeFilter === "all" || c.urgency === activeFilter) && (!q || `${c.name}${c.code}${c.business}`.toLowerCase().includes(q)));
  companyList.innerHTML = visible.map((c, index) => `
    <article class="company-row" tabindex="0" data-code="${c.code}">
      <div class="company-main"><span class="company-logo" style="background:${colors[index % colors.length]}">${c.name.replace(/株式会社/g, "").slice(0, 2)}</span><div><strong>${c.name} <em class="data-badge ${c.data_kind}">${c.data_kind === "actual" ? "実データ" : "デモ"}</em></strong><small>${c.code} · ${c.market}</small></div></div>
      <div class="metric"><small>現預金ランウェイ</small><strong>${c.runway_months ?? "—"}ヶ月</strong></div>
      <div class="metric"><small>営業CF（YoY）</small><strong class="negative">${c.cf_change_percent ?? "—"}%</strong></div>
      <span class="risk ${c.urgency}">${urgencyLabel[c.urgency]}</span><button class="row-arrow" aria-label="${c.name}の詳細">›</button>
    </article>`).join("") || '<p class="empty-result">条件に一致する企業はありません。</p>';
  document.querySelectorAll(".company-row").forEach(row => row.addEventListener("click", () => showCompany(row.dataset.code)));
}
async function loadSignals() {
  companyList.innerHTML = '<p class="loading">財務シグナルを計算中…</p>';
  try {
    const response = await fetch("/api/signals");
    if (!response.ok) throw new Error(`API error ${response.status}`);
    const payload = await response.json(); companies = payload.data; renderCompanies();
    document.querySelector(".data-status").innerHTML = "<i></i>SAMPLE API";
  } catch (error) {
    companyList.innerHTML = '<p class="api-error">APIへ接続できません。<code>python3 server.py</code> で起動してください。</p>';
    document.querySelector(".data-status").textContent = "API OFFLINE";
  }
}
function showCompany(code) {
  const c = companies.find(item => item.code === code); if (!c) return;
  const value = (number, suffix = "") => number == null ? "取得なし" : `${number.toLocaleString()}${suffix}`;
  const sources = c.sources?.map(s => `<tr><td>${s.metric}</td><td>${value(s.value)} ${s.unit}</td><td>${s.period_start || "—"}〜${s.period_end || "—"}</td><td>${s.scope === "consolidated" ? "連結" : "単体"}</td><td><code>${s.doc_id}</code></td></tr>`).join("") || "";
  document.querySelector("#companyDetailContent").innerHTML = `<p class="eyebrow">${c.code} · ${c.market} <em class="data-badge ${c.data_kind}">${c.data_kind === "actual" ? "EDINET実データ" : "デモデータ"}</em></p><h2>${c.name}</h2><p>${c.business}</p><div class="detail-metrics"><div><small>リスクスコア</small><strong>${c.score == null ? "判定不可" : `${c.score}/100`}</strong></div><div><small>現預金</small><strong>${value(c.cash_million, "百万円")}</strong></div><div><small>自己資本比率</small><strong>${value(c.equity_ratio, "%")}</strong></div><div><small>有利子負債</small><strong>${value(c.interest_debt_million, "百万円")}</strong></div></div><details class="methodology"><summary>スコア計算条件</summary><p>ランウェイ6か月未満: 60点、12か月未満: 35点、営業CFが前年比15%以上悪化: 25点、自己資本比率低下: 15点。60点以上を高、35点以上を中とします。これは調査対象の優先順位であり、増資の必要性を断定するものではありません。</p></details><h3>確認対象となった理由</h3><ul>${c.reasons.map(r => `<li>${r}</li>`).join("") || "<li>スコア条件に該当する理由はありません</li>"}</ul>${c.missing?.length ? `<div class="missing-note"><strong>判定に不足する項目</strong><p>${c.missing.join("、")}。値をゼロ補完せず判定不可としています。</p></div>` : ""}${sources ? `<h3>数値の出典</h3><div class="source-table"><table><thead><tr><th>項目</th><th>原値・単位</th><th>対象期間</th><th>区分</th><th>書類ID</th></tr></thead><tbody>${sources}</tbody></table></div><p class="source-note">取得日時: ${c.sources[0].acquired_at}</p>` : `<p class="source-note">デモデータであり、実在する企業・開示書類の値ではありません。</p>`}`;
  document.querySelector("#companyDetail").showModal();
}
loadSignals();

document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".filter").forEach(b => b.classList.remove("active")); button.classList.add("active"); activeFilter = button.dataset.filter; renderCompanies();
}));

const titles = { signals:"資金需要シグナル", companies:"企業データベース", ma:"M&A・投資動向", matching:"マッチング", reports:"レポート", watchlist:"ウォッチリスト", alerts:"アラート設定" };
function setView(view) {
  document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === view));
  const dashboard = document.querySelector("#dashboard"), secondary = document.querySelector("#secondaryView");
  dashboard.hidden = view !== "dashboard"; secondary.hidden = view === "dashboard";
  if (view !== "dashboard") document.querySelector("#secondaryTitle").textContent = titles[view] || "インテリジェンス";
  document.querySelector(".sidebar").classList.remove("open"); window.scrollTo({top:0,behavior:"smooth"});
}
document.querySelectorAll("[data-view], [data-view-link]").forEach(el => el.addEventListener("click", e => { e.preventDefault(); setView(el.dataset.view || el.dataset.viewLink); }));
document.querySelector(".menu-toggle").addEventListener("click", () => document.querySelector(".sidebar").classList.toggle("open"));
document.querySelector("#newScreening").addEventListener("click", () => document.querySelector("#screeningDialog").showModal());
document.querySelector("#refreshButton").addEventListener("click", e => { e.currentTarget.classList.add("spinning"); setTimeout(() => e.currentTarget.classList.remove("spinning"), 650); const toast=document.querySelector("#toast"); toast.classList.add("show"); setTimeout(()=>toast.classList.remove("show"),2600); });
document.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); document.querySelector("#globalSearch").focus(); } });
document.querySelector("#globalSearch").addEventListener("input", renderCompanies);
