// 用 node fetch 抓壳页, 提取 loader JS 全文供逆向分析
const url = process.argv[2];
(async () => {
  try {
    const r = await fetch(url, { redirect: 'manual',
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36' } });
    const body = await r.text();
    console.error('status:', r.status, 'len:', body.length);
    const m = body.match(/<script>([\s\S]*?)<\/script>/);
    if (m) {
      require('fs').writeFileSync('/tmp/loader_full.js', m[1]);
      console.error('loader JS 长度:', m[1].length);
    } else {
      console.error('无 script; body 前 300:', body.slice(0, 300));
    }
    console.error('Set-Cookie:', r.headers.get('set-cookie') || '(无)');
  } catch (e) {
    console.error('ERR:', e.message);
  }
})();
