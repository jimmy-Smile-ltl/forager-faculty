// loader 变体实验: 西交/北理工系站首访 200 + "正在加载" 壳(非 412),
// 壳 JS 动态验证后跳转/重载同 URL。用 sdenv jsdom 跑壳 → 不阻止跳转 →
// 带 cookieJar 二次抓取看是否拿到真实内容。
const { jsdomFromUrl, logger } = require('sdenv');
logger.level.level = 50000;
const url = process.argv[2];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36';
(async () => {
  try {
    const dom = await jsdomFromUrl(url, {
      strictSSL: false, userAgent: UA, runScripts: 'dangerously',
      resources: 'usable', browserType: 'chrome',
    });
    const win = dom.window;
    // 等 loader 的 maxWaitTime(~10s) + 缓冲
    await new Promise(r => setTimeout(r, 15000));
    const cookies = dom.cookieJar.getCookieStringSync(url);
    console.error('cookieJar:', cookies.length, 'chars:', cookies.slice(0, 120));
    // 带 cookie 二次抓取
    const r2 = await fetch(url, { headers: { cookie: cookies, 'User-Agent': UA } });
    const t2 = await r2.text();
    console.error('二次抓取:', t2.length, 'B');
    process.stdout.write(JSON.stringify({ ok: t2.length > 10000, len: t2.length, cookie: cookies, html: t2.slice(0, 300) }));
    win.close(); process.exit(0);
  } catch (e) {
    console.error('ERR:', e.message);
    process.exit(1);
  }
})();
