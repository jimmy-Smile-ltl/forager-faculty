#!/usr/bin/env node
/**
 * generate_cookie.js — sdenv 补环境瑞数 Cookie 生成器（通用方案）
 *
 * 策略:
 *   1. 首次 https 探测: 若直接 200 → 无瑞数拦截, 返回 no-ruishu
 *   2. jsdomFromUrl 全浏览器模拟 (strictSSL:false + chrome 补环境 +
 *      location.replace 拦截 + timer 容错) → 生成完整 Cookie (O+P 两个)
 *   3. 若 jsdomFromUrl 因第三方 JS 崩溃 → 手动 VMP: 提取 412 页面脚本
 *      在干净 jsdom 中只执行 VMP, 生成基础 Cookie
 *
 * 用法:
 *   node generate_cookie.js --url <URL> [--wait 10] [--output <FILE>]
 */
const fs = require('fs');
const path = require('path');
const https = require('https');
const { jsdomFromUrl, jsdomFromText } = require('sdenv');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36';

function parseArgs() {
    const args = {};
    const argv = process.argv.slice(2);
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        const m = arg.match(/^--(\w[\w-]*)=(.*)$/);
        if (m) { args[m[1]] = m[2]; continue; }
        const m2 = arg.match(/^--(\w[\w-]*)$/);
        if (m2 && i + 1 < argv.length && !argv[i + 1].startsWith('--')) {
            args[m2[1]] = argv[i + 1];
            i++;
            continue;
        }
        if (!arg.startsWith('--') && !args.url) args.url = arg;
    }
    return args;
}

function httpGet(url, cookieStr) {
    // 2026-10-16: 支持 http/https 双协议(成都理工 hgycg 名单 http 实锤,
    // 原写死 https.get 对 http URL 直接 reject)
    const mod = url.startsWith('https://') ? https : http;
    return new Promise((resolve, reject) => {
        const headers = { 'User-Agent': UA };
        if (cookieStr) headers['Cookie'] = cookieStr;
        const opts = { headers };
        if (mod === https) opts.rejectUnauthorized = false;
        mod.get(url, opts, resp => {
            let data = '';
            resp.on('data', c => data += c);
            resp.on('end', () => resolve({ body: data, headers: resp.headers, status: resp.statusCode }));
        }).on('error', reject);
    });
}

function isRsCookie(cookies) {
    // 瑞数 token 特征: 值 > 60 字符, 且不是 session/普通 cookie
    const SESSION_NAMES = /^(JSESSIONID|PHPSESSID|ASP\.NET_SessionId|sessionid|sid)$/i;
    return cookies.some(c =>
        c.value.length > 60 && !SESSION_NAMES.test(c.name)
    );
}

function parseCookies(str) {
    if (!str) return [];
    return str.split(';').map(c => c.trim()).filter(Boolean).map(c => {
        const eq = c.indexOf('=');
        return eq === -1 ? { name: c, value: '' } : { name: c.substring(0, eq).trim(), value: c.substring(eq + 1).trim() };
    });
}

/**
 * 方案 2: jsdomFromUrl 完整浏览器模拟
 */
async function fullSimulate(url, waitSec) {
    return new Promise((resolve) => {
        let resolved = false;
        let lastDom = null;

        // 捕获进程级异常，防止 img/第三方JS 错误杀进程
        const ueHandler = (err) => {
            console.error('   ⚠️ 页面异常(已捕获): ' + err.message);
        };
        process.on('uncaughtException', ueHandler);

        jsdomFromUrl(url, {
            strictSSL: false,
            userAgent: UA,
            runScripts: 'dangerously',
            resources: 'usable',
            browserType: 'chrome',
            beforeParse(window) {
                const st = window.setTimeout;
                const si = window.setInterval;
                window.setTimeout = function(fn, d) {
                    return st(function() { try { fn(); } catch(e) {} }, d);
                };
                window.setInterval = function(fn, d) {
                    return si(function() { try { fn(); } catch(e) {} }, d);
                };
                window.location.replace = function(newUrl) {
                    console.error('[BLOCKED] location.replace → ' + newUrl);
                };
            },
        }).then(dom => {
            lastDom = dom;
            const win = dom.window;
            const cookieJar = dom.cookieJar;

            setTimeout(() => {
                if (resolved) return;
                resolved = true;
                process.removeListener('uncaughtException', ueHandler);
                let cookieStr = '';
                try {
                    cookieStr = cookieJar.getCookieStringSync(url);
                } catch(e) {}
                try { win.close(); } catch(e) {}
                resolve({ cookieStr, error: null });
            }, waitSec * 1000);

        }).catch(err => {
            if (resolved) return;
            resolved = true;
            process.removeListener('uncaughtException', ueHandler);
            // 崩溃前尝试收集 cookie
            let cookieStr = '';
            try {
                if (lastDom) cookieStr = lastDom.cookieJar.getCookieStringSync(url);
            } catch(e) {}
            resolve({ cookieStr, error: err.message });
        });

        setTimeout(() => {
            if (resolved) return;
            resolved = true;
            process.removeListener('uncaughtException', ueHandler);
            let cookieStr = '';
            try {
                if (lastDom) cookieStr = lastDom.cookieJar.getCookieStringSync(url);
            } catch(e) {}
            resolve({ cookieStr, error: '超时' });
        }, (waitSec + 20) * 1000);
    });
}

/**
 * 方案 3: 手动 VMP (从 412 页面提取脚本, 干净环境执行)
 */
async function manualVmp(url, firstResp, waitSec) {
    // 提取 VMP JS URL
    const vmpMatch = firstResp.body.match(/src="(\/(?:[A-Za-z0-9]+\/)+[A-Za-z0-9]+\.\w+\.js)"/)
        || firstResp.body.match(/src="([^"]+\.js)"/);
    if (!vmpMatch) return { cookieStr: '', error: 'no-vmp-js' };

    const vmpUrl = new URL(vmpMatch[1], url).href;
    const rVmp = await httpGet(vmpUrl);

    // ★ 关键: 按 HTML 原始顺序重建脚本序列
    // 真实结构: 内联($_ts配置) → 外部(VMP JS定义函数) → 内联(_$$x()调用)
    // 之前把所有内联都放 VMP 前面, 调用先于定义 → "$_xx is not a function"
    const scripts = [];
    const reAll = /<script[^>]*src="([^"]+)"[^>]*><\/script>|<script[^>]*>([\s\S]*?)<\/script>/gi;
    let m;
    while ((m = reAll.exec(firstResp.body)) !== null) {
        if (m[1]) {
            // 外部脚本 → 替换为 VMP 内容
            scripts.push({ type: 'external', src: m[1] });
        } else if (m[2] && m[2].trim() && !m[2].trim().startsWith('document.createElement')) {
            scripts.push({ type: 'inline', code: m[2].trim() });
        }
    }

    const htmlParts = [];
    for (const s of scripts) {
        if (s.type === 'inline') {
            htmlParts.push('<script>' + s.code + '</script>');
        } else {
            // 外部 VMP JS 内联化
            htmlParts.push('<script>' + rVmp.body + '</script>');
        }
    }

    // ★ 瑞数 6 代: <head> 中的 <meta r='m'> 是 VMP 解密密钥, 必须保留
    const headMatch = firstResp.body.match(/<head[^>]*>([\s\S]*?)<\/head>/i);
    let headHtml = '';
    if (headMatch) {
        const metas = [];
        const metaRe = /<meta[^>]*>/gi;
        let mm;
        while ((mm = metaRe.exec(headMatch[1])) !== null) {
            metas.push(mm[0]);
        }
        headHtml = metas.join('\n');
    }

    const html = '<!DOCTYPE html><html><head><meta charset="utf-8">\n'
        + headHtml + '\n'
        + htmlParts.join('\n')
        + '\n</head><body></body></html>';

    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), (waitSec + 30) * 1000);

    try {
        const dom = await jsdomFromText(html, {
            url, referrer: url, contentType: 'text/html',
            runScripts: 'dangerously', pretendToBeVisual: true, userAgent: UA,
            signal: ctrl.signal,
            beforeParse(window) {
                const sc = [].concat(firstResp.headers['set-cookie'] || []);
                for (const c of sc) {
                    try { window.document.cookie = c.split(';')[0]; } catch(e) {}
                }
                try {
                    window.location.replace = function(nu) {
                        console.error('[BLOCKED] location.replace → ' + nu);
                    };
                } catch(e) {}
            },
        });
        clearTimeout(t);
        await new Promise(r => setTimeout(r, waitSec * 1000));
        const cookieStr = dom.cookieJar.getCookieStringSync(url);
        dom.window.close();
        return { cookieStr, error: null };
    } catch (err) {
        clearTimeout(t);
        return { cookieStr: '', error: err.message };
    }
}

async function generateCookies(url, waitSec, outputFile) {
    // 2026-10-16: jsdom(jsdomFromUrl) 只支持 https — http 名单 URL 升级
    // https(成都理工 hgycg.cdut.edu.cn http 名单但 https 实测可达)。
    if (url.startsWith('http://')) {
        url = 'https://' + url.slice('http://'.length);
    }
    console.error('🔐 ' + url);

    // 1. 探测是否瑞数
    const r1 = await httpGet(url);
    console.error('   首次响应: ' + r1.status);

    if (r1.status === 200 && r1.body.length > 5000) {
        console.error('   ✅ 无瑞数拦截 (200)');
        const result = { url, cookies: [], cookieString: '', success: false, error: 'no-ruishu', attempts: 1 };
        const json = JSON.stringify(result, null, 2);
        if (outputFile) fs.writeFileSync(outputFile, json);
        else process.stdout.write(json);
        return result;
    }

    // 2. jsdomFromUrl 完整模拟 (首选)
    console.error('   📡 全浏览器模拟 (wait=' + waitSec + 's)...');
    const r2 = await fullSimulate(url, waitSec);
    const r2Cookies = parseCookies(r2.cookieStr);
    if (isRsCookie(r2Cookies)) {
        console.error('   ✅ 全模拟成功: ' + r2.cookieStr.length + ' chars');
        const result = { url, cookies: r2Cookies, cookieString: r2.cookieStr, success: true, error: null, attempts: 2 };
        const json = JSON.stringify(result, null, 2);
        if (outputFile) fs.writeFileSync(outputFile, json);
        else process.stdout.write(json);
        console.error('   ✅ 完成');
        return result;
    }
    console.error('   ⚠️ 全模拟未获瑞数token: ' + (r2.error || 'cookie过短'));

    // 3. 手动 VMP 兜底
    console.error('   🔧 手动VMP兜底...');
    const r3 = await manualVmp(url, r1, waitSec);
    const cookies = parseCookies(r3.cookieStr);
    const result = {
        url, cookies, cookieString: r3.cookieStr,
        success: isRsCookie(cookies),
        error: r3.error,
        attempts: 3,
    };
    const json = JSON.stringify(result, null, 2);
    if (outputFile) fs.writeFileSync(outputFile, json);
    else process.stdout.write(json);
    console.error(result.success ? '   ✅ 完成(兜底)' : '   ❌ ' + (r3.error || 'cookie过短'));
    return result;
}

module.exports = { generateCookies };

// ── CLI ────────────────────────────────────────────
if (require.main === module) {
    (async () => {
        const args = parseArgs();
        if (!args.url) {
            console.error('用法: node generate_cookie.js --url <URL> [--wait 10] [--output <FILE>]');
            process.exit(1);
        }
        let url = args.url;
        if (!url.startsWith('http')) url = 'https://' + url;
        const waitSec = parseInt(args.wait || '10');
        const output = args.output || '';

        try {
            await generateCookies(url, waitSec, output);
        } catch (err) {
            console.error('❌ ' + err.message);
            process.exit(1);
        }
    })();
}
