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
//   node check.js --watch         幹事長会・議会運営委員会だけを軽く確認（平日8〜18時に1時間ごと。2026-11-25 先生の指示）

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
const WATCH = args.has('--watch');

// ---------- 目立たせる会議（2026-11-25 先生の指示：当日に資料が上がり、意見書を作ることがある） ----------
const WATCH_RE = /幹事長会|議会運営委員会/;
const isWatchedPath = (p) => WATCH_RE.test(p || '');
const LOCK = path.join(HERE, '.running');

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
    // 年度のフォルダは「要点記録(H29～) / 令和6年度_要点記録」のように深い所にもあるので、3階層目より下をすべて見る
    if (/懇談会/.test(b) && segs.slice(2).some((s) => /年度/.test(s) && !yrs.has(reiwaNum(s)))) return false;
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
    // 詳細欄が開ききるまで、ボタンが出るのを最大15秒待つ（2026-11-20：待ち不足で42件取りこぼしたため）
    let visible = [];
    for (let t = 0; t < 30 && !visible.length; t++) {
      if (t) await sleep(500);
      const c = await btn.count();
      for (let j = 0; j < c; j++) if (await btn.nth(j).isVisible().catch(() => false)) visible.push(btn.nth(j));
    }
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

// ---------- LINEの文面（2026-11-20 先生「見にくい」→ フォルダごとにまとめる） ----------
// 長い住所を1行ずつ並べず、いちばん上のフォルダ→その中の場所→資料名の順にまとめる。
// 同じ場所に5件以上あるときは3件だけ書いて「ほか○件」。詳しい一覧は log.txt に残る。
const TOP_ORDER = [/^04|幹事長会/, /定例議会|臨時会/, /^03/, /^02/, /^01/, /^06/, /^07/, /^05/, /^08/, /^10/, /^11/, /^12/, /^09/];
const topRank = (t) => { const i = TOP_ORDER.findIndex((re) => re.test(t)); return i < 0 ? 99 : i; };
const shortTop = (t) => t.replace(/^\d+[\s_]*/, '');

function formatLineMessage({ added, changed, missed, files, dl, started }) {
  const md = started.toLocaleDateString('ja-JP', { timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric' });
  const head = `【SideBooks 新着】${md}　${added.length}件` + (changed.length ? `（更新${changed.length}件）` : '');
  const groups = {};
  const put = (k, mark) => {
    const f = files[k];
    const segs = (f.folder || '').split(' / ').filter(Boolean);
    const top = segs[0] || 'ホーム';
    const sub = segs.slice(1, 3).join('／'); // 細かく分かれすぎないよう、上から3階層目までで区切る
    ((groups[top] ||= {})[sub] ||= []).push(f.name + mark);
  };
  added.forEach((k) => put(k, ''));
  changed.forEach((k) => put(k, '（更新）'));

  const out = [head];
  for (const top of Object.keys(groups).sort((a, b) => topRank(a) - topRank(b) || a.localeCompare(b, 'ja'))) {
    const subs = groups[top];
    const n = Object.values(subs).reduce((s, a) => s + a.length, 0);
    out.push('', `◆${shortTop(top)}（${n}件）`);
    for (const sub of Object.keys(subs).sort((a, b) => a.localeCompare(b, 'ja'))) {
      const names = subs[sub];
      if (sub) out.push(`▶${sub}`);
      const show = names.length >= 5 ? names.slice(0, 3) : names;
      show.forEach((x) => out.push(`・${x}`));
      if (show.length < names.length) out.push(`　ほか${names.length - show.length}件`);
    }
  }
  if (missed && missed.length) out.push('', `※新着ファイル画面に出ていなかったもの：${missed.length}件（PCの log.txt に一覧）`);
  if (dl) {
    out.push('', `💾 保存 ${dl.ok}件 → マイドライブ「SideBooks新着」の ${dl.day}`);
    if (dl.ng) out.push(`　保存できなかったもの ${dl.ng}件は、次の回にやり直します`);
    if (dl.gaveUp.length) out.push(`　5回試しても保存できなかったもの（SideBooksで直接）：`, ...dl.gaveUp.map((x) => `　・${x.split('：')[0]}`));
  }
  return out.join('\n');
}

// 幹事長会・議会運営委員会の新着は、ふだんのまとめとは別の1通で目立たせる
function formatAlertMessage(keys, files, dl) {
  const groups = {};
  for (const k of keys) {
    const f = files[k];
    const segs = (f.folder || '').split(' / ').filter(Boolean);
    const i = segs.findIndex((x) => WATCH_RE.test(x));
    const meeting = i >= 0 ? (segs[i].match(WATCH_RE) || [''])[0] : '会議';
    const where = segs.slice(i + 1).join('／') || segs.slice(-1).join('');
    ((groups[meeting] ||= {})[where] ||= []).push(f.name);
  }
  const out = [];
  for (const meeting of Object.keys(groups).sort((a, b) => (a === '幹事長会' ? -1 : b === '幹事長会' ? 1 : 0))) {
    out.push(`🚨🚨【${meeting}】資料が上がりました🚨🚨`);
    for (const [where, names] of Object.entries(groups[meeting])) {
      if (where) out.push(`▶${where}`);
      names.forEach((n) => out.push(`・${n}`));
    }
    out.push('');
  }
  if (dl) out.push(dl.ok ? `💾 マイドライブ「SideBooks新着」の ${dl.day} に保存済み` : '💾 保存できませんでした。SideBooksで直接見てください');
  out.push('意見書を作るなら、Claudeに「幹事長会の資料から意見書の骨子を」と一言。');
  return out.join('\n').trim();
}

// 新着はないが、やり直しても保存できずにあきらめた資料がある日の知らせ
function formatGaveUpMessage(gaveUp, started) {
  const md = started.toLocaleDateString('ja-JP', { timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric' });
  return [`【SideBooks】${md}　新着はありません`, '', '5回試しても保存できなかった資料があります。SideBooksで直接ご覧ください。',
    ...gaveUp.map((x) => `・${x.split('：')[0]}`)].join('\n');
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

// ---------- 軽い確認（--watch）：幹事長会・議会運営委員会だけ ----------
// 見るのは「新着ファイル画面」と、記録にある該当フォルダのうち日付が近いもの・年度のまとめフォルダだけ（1回で数画面）
const folderDate = (name) => {
  const m = String(name).match(/R\s*(\d+)\.(\d+)\.(\d+)/);
  return m ? new Date(Date.UTC(2018 + Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : null;
};
function watchTargets(prev) {
  const today = jstNow();
  const t0 = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const out = [];
  for (const f of prev.folders || []) {
    if (!f.seq || !f.path || !isWatchedPath(f.path)) continue;
    const segs = f.path.split(' / ');
    const d = folderDate(segs[segs.length - 1]);
    if (d) {
      const diff = (d.getTime() - t0) / 86400000;
      if (diff >= -3 && diff <= 14) out.push(f); // 会議の日が3日前〜2週間後のフォルダ
    } else if (inScope(segs, 'daily') && (segs.length === 1 || segs.some((x) => activeReiwaYears().has(reiwaNum(x))))) {
      out.push(f); // 「令和8年度」など、新しい日付フォルダができる親フォルダ
    }
  }
  return out.sort((a, b) => a.path.split(' / ').length - b.path.split(' / ').length).slice(0, 15);
}

async function watchRun(page, prev, started) {
  const known = new Set(Object.values(prev.files).map((v) => v.seq).filter(Boolean));
  const knownFolders = new Set((prev.folders || []).map((f) => f.path));
  const found = {};
  const newFolders = [];
  const queue = watchTargets(prev).map((f) => ({ seq: f.seq, path: f.path }));
  log(`軽い確認：フォルダ ${queue.length}か所＋新着ファイル画面`);
  const seen = new Set();
  while (queue.length && seen.size < 25) {
    const q = queue.shift();
    if (seen.has(q.seq)) continue;
    seen.add(q.seq);
    const r = await openFolder(page, q.seq);
    for (const f of r.files) {
      if (f.seq && known.has(f.seq)) continue;
      const k = `${q.path} / ${f.name}`;
      if (!prev.files[k]) found[k] = { folder: q.path, folseq: q.seq, name: f.name, seq: f.seq, updated: f.updated, pages: f.pages, via: 'watch' };
    }
    for (const f of r.folders) {
      const p = `${q.path} / ${f.name}`;
      if (knownFolders.has(p) || !f.seq) continue;
      newFolders.push({ seq: f.seq, path: p, folders: 0, files: 0 });
      queue.push({ seq: f.seq, path: p }); // 新しくできた日付フォルダは中まで見る
    }
  }
  try {
    const nw = await readNewPage(page, known);
    for (const x of nw.items) {
      if (!x.seq || known.has(x.seq) || !isWatchedPath(x.folder)) continue;
      if (Object.values(found).some((v) => v.seq === x.seq)) continue;
      found[`${x.folder} / ${x.name}`] = { folder: x.folder, folseq: 'new', name: x.name, seq: x.seq, updated: x.updated, pages: x.pages, via: 'new' };
    }
  } catch (e) { log('注意：新着ファイル画面を読めませんでした'); }

  const keys = Object.keys(found).sort();
  if (!keys.length) { console.log('幹事長会・議会運営委員会の新着なし'); return; }
  keys.forEach((k) => console.log('【目立たせる新着】' + k));

  let dl = null;
  if (!NO_DOWNLOAD) {
    const day = started.toLocaleDateString('sv-SE', { timeZone: 'Asia/Tokyo' });
    const dir = path.join(saveRoot(), day);
    let ok = 0;
    for (const k of keys) { const r = await downloadOne(page, found[k], dir); if (r.ok) ok++; await sleep(WAIT_MS); }
    dl = { ok, day };
    console.log(`保存：${ok}件（${dir}）`);
  }
  // 見つけたものだけ記録に足す（ほかの新着は、翌朝のふだんの確認で知らせる）
  const merged = { ...prev, files: { ...prev.files, ...found }, folders: [...(prev.folders || []), ...newFolders], watch_at: started.toISOString() };
  fs.writeFileSync(SNAPSHOT, JSON.stringify(merged, null, 1));
  if (!NO_LINE) await sendLine(formatAlertMessage(keys, found, dl));
}

// 朝の確認と1時間ごとの確認が重ならないようにする
function takeLock(waitForIt) {
  const fresh = () => { try { return Date.now() - fs.statSync(LOCK).mtimeMs < 3 * 3600 * 1000; } catch (e) { return false; } };
  return (async () => {
    for (let i = 0; fresh(); i++) {
      if (!waitForIt || i >= 40) return false;
      await sleep(30000);
    }
    fs.writeFileSync(LOCK, String(process.pid));
    return true;
  })();
}
const releaseLock = () => { try { fs.unlinkSync(LOCK); } catch (e) {} };

// ---------- 本体 ----------
async function main() {
  if (!TEST) {
    if (!(await takeLock(!WATCH))) { console.log('ほかの確認が動いているので、今回は見送りました'); return; }
    process.on('exit', releaseLock);
  }
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
    if (WATCH) {
      if (!prev) { console.log('まだ最初の記録がないので、軽い確認はしません（先に manual.bat を実行）'); return; }
      await watchRun(page, prev, started);
      return;
    }
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
    // 保存できなかった資料は「やり直し待ち」に残し、次の回にもう一度保存を試みる（5回まで）
    const pending = { ...(prev.pending_downloads || {}) };
    for (const k of Object.keys(pending)) if (!mergedFiles[k]) delete pending[k];
    const merged = { ...snap, folder_count: mergedFolders.length, file_count: Object.keys(mergedFiles).length, folders: mergedFolders, files: mergedFiles, pending_downloads: pending };
    fs.writeFileSync(SNAPSHOT, JSON.stringify(merged, null, 1));

    const retryKeys = NO_DOWNLOAD ? [] : Object.keys(pending).filter((k) => !added.includes(k));
    if (!added.length && !changed.length && !retryKeys.length) {
      console.log('新着なし');
      warnings.forEach((w) => log('注意：' + w));
      return;
    }

    const lines = [];
    added.forEach((k) => lines.push(k));
    changed.forEach((k) => lines.push(`${k}（更新）`));
    lines.forEach((l) => console.log(l));

    // 確かめたいこと：フォルダには入ったのに新着ファイル画面に出なかった資料（閲覧者を限定した資料など）
    let missed = [];
    if (newSeqs) {
      const tenDaysAgo = new Date(Date.now() - 10 * 86400000).toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }).replace(/-/g, '/');
      missed = added.filter((k) => files[k].seq && !newSeqs.has(files[k].seq) && (files[k].updated || '') >= tenDaysAgo);
      if (missed.length) {
        const note = '（新着ファイル画面には出ていなかったもの）\n' + missed.join('\n');
        console.log(note);
        lines.push(note);
      }
    }

    let dlNote = '';
    let dlSummary = null;
    if (!NO_DOWNLOAD && (added.length || retryKeys.length)) {
      const day = started.toLocaleDateString('sv-SE', { timeZone: 'Asia/Tokyo' });
      const dir = path.join(saveRoot(), day);
      let ok = 0; let retried = 0; const ng = []; const gaveUp = [];
      for (const k of [...added, ...retryKeys]) {
        const item = mergedFiles[k];
        const r = await downloadOne(page, item, dir);
        if (retryKeys.includes(k) && r.ok) retried++;
        if (r.ok) { ok++; delete pending[k]; } else {
          pending[k] = (pending[k] || 0) + 1;
          if (pending[k] >= 5) { delete pending[k]; gaveUp.push(`${item.name}：${r.why}`); }
          else if (added.includes(k)) ng.push(`${item.name}：${r.why}`);
        }
        await sleep(WAIT_MS);
      }
      fs.writeFileSync(SNAPSHOT, JSON.stringify({ ...merged, pending_downloads: pending }, null, 1));
      dlNote = `\n保存：${ok}件（${dir}）` + (retried ? `（うち前回までに保存できなかった分 ${retried}件）` : '')
        + (ng.length ? `\n保存できなかったもの（次の回にやり直します）：\n${ng.join('\n')}` : '')
        + (gaveUp.length ? `\n5回試しても保存できなかったもの（SideBooksで直接見てください）：\n${gaveUp.join('\n')}` : '');
      console.log(dlNote.trim());
      dlSummary = { ok, ng: ng.length, gaveUp, day };
    }

    // 新着も更新もなく、やり直しの保存だけだった日は、LINEは送らない（記録だけ）。
    // ただし、5回試してもあきらめた資料があれば、それだけ短く知らせる（2026-11-28 先生の指示）
    if (!added.length && !changed.length) {
      if (!NO_LINE && dlSummary && dlSummary.gaveUp.length) await sendLine(formatGaveUpMessage(dlSummary.gaveUp, started));
      warnings.forEach((w) => log('注意：' + w));
      return;
    }

    if (!NO_LINE) {
      const watched = added.filter((k) => isWatchedPath(files[k].folder));
      if (watched.length) await sendLine(formatAlertMessage(watched, files, dlSummary));
      await sendLine(formatLineMessage({ added, changed, missed, files, dl: dlSummary, started }));
    }
    warnings.forEach((w) => log('注意：' + w));
  } finally {
    await browser.close();
  }
}

// 失敗したときは黙って終わらず、LINEでも知らせる（「新着なし」と「動いていない」を見分けるため）
main().catch(async (e) => {
  let msg = String(e && e.message ? e.message : e).split('\n')[0].slice(0, 200);
  for (const secret of [process.env.SIDEBOOKS_USER, process.env.SIDEBOOKS_PASS, process.env.LINE_CHANNEL_TOKEN]) {
    if (secret) msg = msg.split(secret).join('＊＊＊');
  }
  console.error('エラー：' + msg);
  if (!NO_LINE && !WATCH) { // 1時間ごとの軽い確認の失敗は log.txt だけ（毎時LINEが来ないように）
    try { await sendLine('【SideBooks 新着チェック】今日は動きませんでした。\n理由：' + msg + '\nPCの sidebooks-check\\log.txt に詳しい記録があります。'); } catch (e2) {}
  }
  process.exit(1);
});
