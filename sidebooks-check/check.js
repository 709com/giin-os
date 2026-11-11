// SideBooks Cloud（文京区議会の資料棚）新着ファイル監視
// 見るだけ。アップロード・削除・移動・名前変更はしない。
// 使い方は README.md を参照。
//
//   node check.js            棚全体を確認して新着を報告（2回目以降は新着をダウンロード）
//   node check.js --test     1フォルダ（R8.11.9）だけで動作確認。記録は残さない
//   node check.js --debug    --test と一緒に使うと、画面の作りを debug/ に保存（微調整用）
//   node check.js --no-download   ダウンロードしない
//   node check.js --no-line       LINEに送らない
//   node check.js --full          曜日にかかわらず棚全体を見る（ふだんは毎朝・日曜・第1日曜で見る範囲を変える）

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { chromium } = require('playwright');

const BASE = 'https://bunkyo-gikai.backshelf.jp/';
const TEST_FOLSEQ = 37357; // R8.11.9
const WAIT_MS = Number(process.env.SIDEBOOKS_WAIT_MS || 1500); // 画面を開くごとの間隔
const MAX_FOLDERS = Number(process.env.SIDEBOOKS_MAX_FOLDERS || 3000); // 暴走よけ
const HERE = __dirname;
const SNAPSHOT = path.join(HERE, 'snapshot.json');
const DEBUG_DIR = path.join(HERE, 'debug');

const args = new Set(process.argv.slice(2));
const TEST = args.has('--test');
const DEBUG = args.has('--debug');
const NO_DOWNLOAD = args.has('--no-download') || TEST;
const NO_LINE = args.has('--no-line') || TEST;
const FULL_ARG = args.has('--full');

// ---------- 見る範囲（先生の指定 2026-11-11。先方の負担を減らすため） ----------
//   毎朝  ：「令和○年○月定例議会」（最重要）、「03 その他会議」（※開催終了分・old・議員研修会・議連名簿を除く）、「04 幹事長会 / 今年度」
//   日曜  ：上に加えて「09 … / 調査結果 / 今年(R○)」「09 … / 各種計画等」
//   第1日曜：棚全体（--full でいつでも棚全体）
const jstNow = () => new Date(Date.now() + 9 * 3600 * 1000);
function currentReiwaFY() {
  const d = jstNow();
  const y = d.getUTCMonth() + 1 >= 4 ? d.getUTCFullYear() : d.getUTCFullYear() - 1;
  return y - 2018; // 2026年度 = 令和8年度
}
// 年度の変わり目（4・5月）は、前年度のフォルダにも資料が入るので前年度も見る
function activeReiwaYears() {
  const fy = currentReiwaFY();
  const d = jstNow();
  const ys = new Set([fy, d.getUTCFullYear() - 2018]);
  if (d.getUTCMonth() + 1 <= 5) ys.add(fy - 1);
  return ys;
}
const reiwaNum = (name) => { const m = name.trim().match(/^(?:令和\s*|R\s*)(\d+)/i); return m ? Number(m[1]) : null; };

function todaysLevel() {
  if (FULL_ARG) return 'full';
  const d = jstNow();
  if (d.getUTCDay() !== 0) return 'daily';
  return d.getUTCDate() <= 7 ? 'full' : 'weekly';
}

// segs：ホームからのフォルダ名の並び。そのフォルダ（またはその下）を今日見るかどうか
function inScope(segs, level) {
  if (level === 'full' || segs.length === 0) return true;
  const [a, b, c] = segs.map((s) => s.trim());
  const yrs = activeReiwaYears();
  if (/定例議会|臨時会/.test(a) && /^令和/.test(a)) return true;
  // 「03 その他会議」の中の「※開催終了分」「old」「議員研修会」「議連名簿」は毎朝見ない（第1日曜の棚全体だけ）
  // 「今後の議会運営に係る懇談会」は今年度だけ毎朝見る（過去の年度は第1日曜だけ）
  if (/^03\s*その他会議/.test(a)) {
    if (segs.length === 1) return true;
    if (/開催終了分/.test(b) || /^old$/i.test(b) || /^議員研修会$|^議連名簿$/.test(b)) return false;
    if (/懇談会/.test(b) && segs.length >= 3 && /年度/.test(c) && !yrs.has(reiwaNum(c))) return false;
    return true;
  }
  if (/^04\s*幹事長会/.test(a)) return segs.length === 1 || (/年度/.test(b) && yrs.has(reiwaNum(b)));
  if (level === 'weekly' && /^09\s*各種様式/.test(a)) {
    if (segs.length === 1) return true;
    if (b === '各種計画等') return true;
    if (b === '調査結果') return segs.length === 2 || yrs.has(reiwaNum(c));
  }
  return false;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.error(...a); // 進み具合は標準エラーへ（結果と混ぜない）

// ---------- 認証情報（画面・記録には出さない） ----------
function ask(question, hidden) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stderr, terminal: true });
    if (hidden) {
      rl._writeToOutput = (s) => { if (s.includes(question)) rl.output.write(question); };
    }
    rl.question(question, (ans) => { rl.close(); if (hidden) process.stderr.write('\n'); resolve(ans.trim()); });
  });
}

async function credentials() {
  if (process.env.SIDEBOOKS_DEV_NOLOGIN === '1') return {};
  let user = process.env.SIDEBOOKS_USER || '';
  let pass = process.env.SIDEBOOKS_PASS || '';
  if (!user || !pass) {
    if (!process.stdin.isTTY) throw new Error('SIDEBOOKS_USER / SIDEBOOKS_PASS が登録されていません（README.md の手順2）');
    if (!user) user = await ask('SideBooks のユーザー名: ', false);
    if (!pass) pass = await ask('SideBooks のパスワード（表示されません）: ', true);
  }
  return { user, pass };
}

// ---------- 変更操作を止める安全装置 ----------
// 万一ボタンを押し間違えても、削除・アップロード・移動などの通信はブラウザの外へ出さない。
const BLOCK = /(delete|remove|upload|move|copy|rename|makefolder|mkdir|trash|regist|update|save|memo|edit|insert|setbookmark|addbookmark)/i;
async function installGuard(context) {
  await context.route('**/*', (route) => {
    const req = route.request();
    const u = req.url();
    if (req.method() !== 'GET' && BLOCK.test(u.split('?')[0].split('/').pop() || '')) {
      log('（安全装置）変更につながる通信を止めました:', u.split('?')[0].split('/').pop());
      return route.abort();
    }
    return route.continue();
  });
}

// ---------- ログイン ----------
async function login(page, { user, pass }) {
  await page.goto(BASE + 'login.php', { waitUntil: 'networkidle' });
  await page.fill('#name', user);
  await page.fill('#pass', pass);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
    page.click('form[name="MyLogin"] input[type="submit"]'),
  ]);
  await sleep(WAIT_MS);
  // ログインできていれば、ヘッダーの「ログイン」ボタンが別の表示に変わる
  const stillLogin = await page.$('a.naviButton.user[href="./login.php"]');
  const onLoginPage = page.url().includes('login.php');
  if (stillLogin || onLoginPage) throw new Error('ログインできませんでした（ユーザー名・パスワードを確認してください）');
  log('ログインしました');
}

// ---------- 1フォルダを開いて中身を読む ----------
async function openFolder(page, folseq) {
  const url = folseq == null ? BASE : folseq === 'new' ? `${BASE}?new` : `${BASE}?folseq=${folseq}`;
  await page.goto(url, { waitUntil: 'networkidle' });
  // 一覧が並び終わるのを待つ（「フォルダ:N件 ファイル:M件」の表示を目印にする）
  await page.waitForSelector('.bottomInfo', { timeout: 20000 }).catch(() => {});
  await sleep(800);
  await sleep(WAIT_MS);
  return readFolder(page);
}

async function readFolder(page) {
  return page.evaluate(() => {
    const txt = (el, sel) => { const x = el.querySelector(sel); return x ? x.textContent.trim() : ''; };
    const info = (document.querySelector('.bottomInfo') || {}).textContent || '';
    const m = info.match(/フォルダ\s*[:：]\s*(\d+).*?ファイル\s*[:：]\s*(\d+)/);
    const crumbs = [...document.querySelectorAll('.topicPathInnner li')].map((li) => li.textContent.trim()).filter(Boolean);
    const here = (window.g_folseq !== undefined && window.g_folseq !== null) ? String(window.g_folseq) : '';

    // 箱（フォルダ・ファイル）の番号を、要素の属性や jQuery の付帯情報から探す
    const findSeq = (el, kind) => {
      // 実際の画面では id="objFolder_36123" / id="objFile_15780" の形で番号が入っている
      const idm = (el.id || '').match(kind === 'folder' ? /^objFolder_(\d+)$/ : /^objFile_(\d+)$/);
      if (idm) return idm[1];
      const cands = {};
      for (const a of el.attributes) cands[a.name] = a.value;
      try { if (window.jQuery) Object.assign(cands, window.jQuery(el).data() || {}); } catch (e) {}
      const keys = Object.keys(cands);
      const pick = (re) => { const k = keys.find((k) => re.test(k)); return k ? String(cands[k]) : ''; };
      if (kind === 'folder') return pick(/fol.*seq|folder.*id|folseq/i) || '';
      return pick(/file.*seq|fileseq|fseq|book.*id|file.*id/i) || '';
    };

    const boxes = [...document.querySelectorAll('#bookShelfContents .box')]
      .filter((b) => b.id !== 'folderTemplate' && b.id !== 'fileTemplate');
    const folders = [];
    const files = [];
    boxes.forEach((b, i) => {
      const name = txt(b, '.fileNameForList') || txt(b, '.fileName');
      if (!name) return;
      const ver = txt(b, '.atrVER');
      if (b.classList.contains('isFolder')) {
        folders.push({ index: i, name, seq: findSeq(b, 'folder'), created: ver });
      } else if (b.classList.contains('isFile')) {
        files.push({ index: i, name, seq: findSeq(b, 'file'), updated: ver, pages: txt(b, '.atrPAGE'), isNew: (b.querySelector('.newIcon') || {}).style?.display !== 'none' });
      }
    });
    return { here, crumbs, info: info.trim(), expect: m ? { folders: +m[1], files: +m[2] } : null, folders, files };
  });
}

// 番号が要素から読めなかったフォルダは、実際にクリックして移動先の番号を読む
async function resolveFolderSeqByClick(page, parentSeq, folder) {
  const boxes = page.locator('#bookShelfContents .box.isFolder:not(#folderTemplate)');
  const n = await boxes.count();
  for (let i = 0; i < n; i++) {
    const b = boxes.nth(i);
    const name = ((await b.locator('.fileNameForList').textContent().catch(() => '')) || (await b.locator('.fileName').textContent().catch(() => '')) || '').trim();
    if (name !== folder.name) continue;
    await Promise.all([
      page.waitForURL(/folseq=\d+/, { timeout: 15000 }).catch(() => {}),
      b.locator('.imgBox').click(),
    ]);
    const m = page.url().match(/folseq=(\d+)/);
    const seq = m && m[1] !== String(parentSeq) ? m[1] : '';
    return seq;
  }
  return '';
}

// ---------- 「新着ファイル」画面（?new） ----------
// SideBooks の新着画面には、先生が見られる資料のうち直近10日分が並ぶ（2026-11-11 確認）。
// 置き場所は一覧に出ないので、詳細（ⓘ）を開いて「場所」の欄を読む（画面の表示だけで、通信は増えない）。
// 詳細を開くと一覧が詳細画面に切り替わるので、場所を調べるのは記録にない資料だけ・1件ごとに新着画面を開き直す。
async function readNewPage(page, knownSeqs) {
  const r = await openFolder(page, 'new');
  const out = [];
  let first = true;
  for (const f of r.files) {
    let where = '';
    if (f.seq && !knownSeqs.has(f.seq)) {
      try {
        if (!first) await openFolder(page, 'new');
        first = false;
        await page.locator(`#objFile_${f.seq} .openDetail`).click({ force: true, timeout: 10000 });
        // 詳細の「ファイルID」がこの資料の番号になり、「場所」が出るまで待つ
        await page.waitForFunction((seq) => {
          const L = [...document.querySelectorAll('body *')]
            .filter((e) => e.offsetParent && e.children.length === 0 && e.textContent.trim())
            .map((e) => e.textContent.trim());
          return L[L.indexOf('ファイルID') + 1] === seq && L.includes('場所');
        }, f.seq, { timeout: 10000 });
        where = await page.evaluate(() => {
          const L = [...document.querySelectorAll('body *')]
            .filter((e) => e.offsetParent && e.children.length === 0 && e.textContent.trim())
            .map((e) => e.textContent.trim());
          const i = L.lastIndexOf('場所');
          return i >= 0 && /^ホーム/.test(L[i + 1] || '') ? L[i + 1] : '';
        });
      } catch (e) { where = ''; }
    }
    const folder = where.replace(/^ホーム\s*(>\s*)?/, '').split(/\s*>\s*/).filter(Boolean).join(' / ');
    out.push({ ...f, folder, located: !!where });
  }
  const unknown = out.filter((x) => x.seq && !knownSeqs.has(x.seq));
  log(`新着ファイル画面：${out.length}件（記録にないもの ${unknown.length}件、うち場所が読めたもの ${unknown.filter((x) => x.located).length}件）`);
  return { info: r.info, items: out };
}

// ---------- 棚をたどる ----------
async function crawl(page, startSeq, startPath, level) {
  const files = {};
  const folderList = [];
  const warnings = [];
  let skipped = 0;
  const queue = [{ seq: startSeq, path: startPath }];
  const seen = new Set();
  while (queue.length) {
    const { seq, path: fpath } = queue.shift();
    const key = seq == null ? 'root' : String(seq);
    if (seen.has(key)) continue;
    seen.add(key);
    if (seen.size > MAX_FOLDERS) { warnings.push(`フォルダ数が上限(${MAX_FOLDERS})を超えたので打ち切りました`); break; }

    const r = await openFolder(page, seq);
    const here = r.here || key;
    const p = fpath; // ホームは空文字、その下は「親 / 子」
    folderList.push({ seq: here, path: p, folders: r.folders.length, files: r.files.length });
    log(`[${seen.size}] ${p || '（ホーム）'}  フォルダ${r.folders.length} ファイル${r.files.length}`);
    if (r.expect && (r.expect.folders !== r.folders.length || r.expect.files !== r.files.length)) {
      warnings.push(`${p || '（ホーム）'}: 画面の件数表示（${r.info}）と読み取った数が違います`);
    }

    for (const f of r.files) {
      const k = p ? `${p} / ${f.name}` : f.name;
      files[k] = { folder: p, folseq: here, name: f.name, seq: f.seq, updated: f.updated, pages: f.pages };
    }
    for (const f of r.folders) {
      if (!inScope([...(p ? p.split(' / ') : []), f.name], level)) { skipped++; continue; }
      let s = f.seq;
      if (!s) {
        s = await resolveFolderSeqByClick(page, here, f);
        await sleep(WAIT_MS);
        if (!s) { warnings.push(`${p ? p + ' / ' : ''}${f.name}: フォルダ番号が読めず、中を見られませんでした`); continue; }
        // クリックで移動したので、次の読み取りのため元のフォルダへ戻る
        await openFolder(page, seq);
      }
      queue.push({ seq: s, path: p ? `${p} / ${f.name}` : f.name });
    }

    if (DEBUG && seen.size === 1) await saveDebug(page);
  }
  return { files, folderList, warnings, skipped };
}

async function saveDebug(page) {
  fs.mkdirSync(DEBUG_DIR, { recursive: true });
  fs.writeFileSync(path.join(DEBUG_DIR, 'folder.html'), await page.content());
  const attrs = await page.evaluate(() => {
    const one = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const o = {};
      for (const a of el.attributes) o[a.name] = a.value;
      try { o.__jqdata = window.jQuery ? window.jQuery(el).data() : null; } catch (e) {}
      return o;
    };
    return {
      folder: one('#bookShelfContents .box.isFolder:not(#folderTemplate)'),
      file: one('#bookShelfContents .box.isFile:not(#fileTemplate)'),
    };
  });
  fs.writeFileSync(path.join(DEBUG_DIR, 'attrs.json'), JSON.stringify(attrs, null, 2));
  await page.screenshot({ path: path.join(DEBUG_DIR, 'folder.png'), fullPage: true });
  log('画面の作りを debug/ に保存しました（GitHubには上がりません）');
}

// ---------- 新着のダウンロード ----------
function saveRoot() {
  if (process.env.SIDEBOOKS_SAVE_DIR) return process.env.SIDEBOOKS_SAVE_DIR;
  if (process.platform === 'win32') {
    for (const d of 'GHIJKLMNOPQRSTUVWXYZDEF') {
      for (const n of ['マイドライブ', 'My Drive']) {
        const p = `${d}:\\${n}`;
        try { if (fs.existsSync(p)) return path.join(p, 'SideBooks新着'); } catch (e) {}
      }
    }
  }
  return path.join(HERE, 'downloads');
}

const safeName = (s) => s.replace(/[\\/:*?"<>|]/g, '_').slice(0, 150);

async function downloadOne(page, item, dir) {
  await openFolder(page, item.folseq);
  // 資料の番号が分かれば番号で、分からなければ名前で探す
  const boxes = item.seq
    ? page.locator(`#objFile_${item.seq}`)
    : page.locator('#bookShelfContents .box.isFile:not(#fileTemplate)');
  const n = await boxes.count();
  for (let i = 0; i < n; i++) {
    const b = boxes.nth(i);
    const name = ((await b.locator('.fileNameForList').textContent().catch(() => '')) || (await b.locator('.fileName').textContent().catch(() => '')) || '').trim();
    if (!item.seq && name !== item.name) continue;
    // 詳細（ⓘ）を開き、「ダウンロード」と書かれたボタンだけを押す
    await b.locator('.openDetail').click({ force: true });
    await sleep(WAIT_MS);
    const btn = page.locator('a, button, input[type="button"]').filter({ hasText: /ダウンロード/ }).filter({ hasNotText: /一括|削除|移動|アップロード/ });
    const visible = [];
    const c = await btn.count();
    for (let j = 0; j < c; j++) if (await btn.nth(j).isVisible().catch(() => false)) visible.push(btn.nth(j));
    if (!visible.length) return { ok: false, why: 'ダウンロードボタンが見つかりません（権限がない可能性）' };
    try {
      const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 60000 }), visible[0].click()]);
      const fname = safeName(dl.suggestedFilename() || item.name);
      const to = path.join(dir, safeName(item.folder.split(' / ').slice(-2).join('_') || 'ホーム'), fname);
      fs.mkdirSync(path.dirname(to), { recursive: true });
      await dl.saveAs(to);
      return { ok: true, to };
    } catch (e) {
      return { ok: false, why: 'ダウンロードが始まりませんでした' };
    }
  }
  return { ok: false, why: 'ファイルが見つかりません' };
}

// ---------- LINE ----------
async function sendLine(text) {
  const token = process.env.LINE_CHANNEL_TOKEN;
  const to = process.env.LINE_USER_ID;
  if (!token || !to) { log('LINEの鍵が登録されていないので、LINEには送りません'); return; }
  const chunks = [];
  for (let s = text; s.length; s = s.slice(4800)) chunks.push(s.slice(0, 4800));
  for (const c of chunks.slice(0, 5)) {
    const r = await fetch('https://api.line.me/v2/bot/message/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ to, messages: [{ type: 'text', text: c }] }),
    });
    if (!r.ok) log('LINEへの送信に失敗しました:', r.status);
  }
}

// ---------- 本体 ----------
async function main() {
  const cred = await credentials();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true, locale: 'ja-JP' });
  await installGuard(context);
  const page = await context.newPage();
  try {
    if (process.env.SIDEBOOKS_DEV_NOLOGIN !== '1') await login(page, cred); // 開発用：ログインせずに画面の作りだけ試す

    if (TEST) {
      const r = await openFolder(page, TEST_FOLSEQ);
      if (DEBUG) await saveDebug(page);
      console.log(`動作確認：${r.crumbs.join(' / ') || 'folseq=' + TEST_FOLSEQ}`);
      console.log(`画面の表示：${r.info}`);
      console.log(`読み取れた数：フォルダ ${r.folders.length}件 ／ ファイル ${r.files.length}件`);
      r.folders.forEach((f) => console.log(`  [フォルダ] ${f.name}  番号:${f.seq || '（読めず→クリックで調べる）'}`));
      r.files.forEach((f) => console.log(`  [ファイル] ${f.name}  更新日:${f.updated || '-'}  ページ数:${f.pages || '-'}`));
      return;
    }

    const started = new Date();
    const prev = fs.existsSync(SNAPSHOT) ? JSON.parse(fs.readFileSync(SNAPSHOT, 'utf8')) : null;
    // 初回・第1日曜・--full は棚全体、ほかの日曜は週1の範囲まで、平日・土曜は毎朝の範囲だけ
    const level = prev ? todaysLevel() : 'full';
    const light = level !== 'full';
    log({ full: '今日は棚全体を見ます', weekly: '今日は毎朝の範囲＋週1回の範囲を見ます', daily: '今日は毎朝の範囲を見ます' }[level]);
    const { files, folderList, warnings, skipped } = await crawl(page, null, '', level);
    if (light) log(`今日は見なかったフォルダ：${skipped}件`);

    // 新着ファイル画面（直近10日分）も毎回見る。たどったフォルダの外に入った資料もここで拾う
    let newSeqs = null;
    try {
      const crawled = new Set(Object.values(files).map((v) => v.seq).filter(Boolean));
      const known = new Set(crawled);
      if (prev) for (const v of Object.values(prev.files)) if (v.seq) known.add(v.seq);
      const nw = await readNewPage(page, known);
      newSeqs = new Set(nw.items.map((x) => x.seq).filter(Boolean));
      for (const x of nw.items) {
        if (x.seq && known.has(x.seq)) continue; // すでに記録にある資料は、記録の場所のままにする
        const folder = x.folder || '（場所不明）';
        files[`${folder} / ${x.name}`] = { folder, folseq: 'new', name: x.name, seq: x.seq, updated: x.updated, pages: x.pages, via: 'new' };
      }
    } catch (e) {
      warnings.push('新着ファイル画面を読めませんでした（フォルダをたどった分だけで判断しました）');
    }

    const snap = { taken_at: started.toISOString(), mode: level, folder_count: folderList.length, file_count: Object.keys(files).length, folders: folderList, files };
    if (!prev) {
      fs.writeFileSync(SNAPSHOT, JSON.stringify(snap, null, 1));
      const top = folderList.filter((f) => f.path && f.path.split(' / ').length <= 2);
      console.log(`初回のため記録を作りました（フォルダ ${folderList.length}件・ファイル ${snap.file_count}件）`);
      console.log('棚の上の方のフォルダ構成：');
      top.forEach((f) => console.log(`  ${f.path}（フォルダ${f.folders}・ファイル${f.files}）`));
      warnings.forEach((w) => console.log('注意：' + w));
      return;
    }

    // 資料の番号で見分ける：別のフォルダ（「開催終了分」など）へ移されただけのものは新着にしない
    const prevBySeq = {};
    for (const [k, v] of Object.entries(prev.files)) if (v.seq) prevBySeq[v.seq] = k;
    const before = (k) => prev.files[k] || (files[k].seq && prev.files[prevBySeq[files[k].seq]]) || null;
    const added = Object.keys(files).filter((k) => !before(k)).sort();
    const changed = Object.keys(files).filter((k) => { const b = before(k); return b && files[k].updated && b.updated && files[k].updated !== b.updated; }).sort();

    // 記録の保存：棚全体を問題なく読めた日は、その日の一覧で置き換える。
    // 一部だけ見た日・読み取りに失敗があった日は、前回の記録を消さないよう合成する（移動したものは古い場所を消す）
    let mergedFiles;
    if (!light && !warnings.length) {
      mergedFiles = files;
    } else {
      const nowSeqs = new Set(Object.values(files).map((v) => v.seq).filter(Boolean));
      mergedFiles = {};
      for (const [k, v] of Object.entries(prev.files)) if (!(v.seq && nowSeqs.has(v.seq) && !files[k])) mergedFiles[k] = v;
      Object.assign(mergedFiles, files);
    }
    let mergedFolders = folderList;
    if (light) {
      const seen = new Set(folderList.map((f) => f.seq));
      mergedFolders = [...folderList, ...(prev.folders || []).filter((f) => !seen.has(f.seq))];
    }
    const merged = { ...snap, folder_count: mergedFolders.length, file_count: Object.keys(mergedFiles).length, folders: mergedFolders, files: mergedFiles };
    fs.writeFileSync(SNAPSHOT, JSON.stringify(merged, null, 1));

    if (!added.length && !changed.length) {
      console.log('新着なし');
      warnings.forEach((w) => log('注意：' + w));
      return;
    }

    const lines = [];
    added.forEach((k) => lines.push(k));
    changed.forEach((k) => lines.push(`${k}（更新）`));
    lines.forEach((l) => console.log(l));

    // 確かめたいこと：フォルダには入ったのに新着ファイル画面に出なかった資料（閲覧者を限定した資料など）
    if (newSeqs) {
      const tenDaysAgo = new Date(Date.now() - 10 * 86400000).toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }).replace(/-/g, '/');
      const missed = added.filter((k) => files[k].seq && !newSeqs.has(files[k].seq) && (files[k].updated || '') >= tenDaysAgo);
      if (missed.length) {
        const note = '（新着ファイル画面には出ていなかったもの）\n' + missed.join('\n');
        console.log(note);
        lines.push(note);
      }
    }

    let dlNote = '';
    if (!NO_DOWNLOAD && added.length) {
      const day = started.toLocaleDateString('sv-SE', { timeZone: 'Asia/Tokyo' });
      const dir = path.join(saveRoot(), day);
      let ok = 0; const ng = [];
      for (const k of added) {
        const r = await downloadOne(page, files[k], dir);
        if (r.ok) ok++; else ng.push(`${files[k].name}：${r.why}`);
        await sleep(WAIT_MS);
      }
      dlNote = `\n保存：${ok}件（${dir}）` + (ng.length ? `\n保存できなかったもの：\n${ng.join('\n')}` : '');
      console.log(dlNote.trim());
    }

    if (!NO_LINE) {
      await sendLine(`【SideBooks 新着】${added.length}件` + (changed.length ? `・更新${changed.length}件` : '') + '\n' + lines.join('\n') + dlNote);
    }
    warnings.forEach((w) => log('注意：' + w));
  } finally {
    await browser.close();
  }
}

main().catch((e) => { console.error('エラー：' + (e && e.message ? e.message : e)); process.exit(1); });
