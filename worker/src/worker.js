// Cloudflare Worker：静态页面之外，额外提供 /api/quotes 行情代理"兜底"。
//
// 正常情况下前端用 JSONP 直连东方财富（走用户自己的国内网络，快且稳），
// 只有直连失败时才退回这里（Cloudflare 海外节点访问东财，可达性一般但可用）。
// public/ 目录下的静态资产会被平台优先匹配，命中不了的路径才进入这段代码。

const EM = "https://push2.eastmoney.com/api/qt/clist/get";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== "/api/quotes") {
      return new Response("Not found", { status: 404 });
    }
    const pn = parseInt(url.searchParams.get("pn") || "1", 10) || 1;
    const pz = Math.min(parseInt(url.searchParams.get("pz") || "100", 10) || 100, 1000);

    // fs 参数里的 "+" 需原样传给东财，故手动拼 URL 而不用 URLSearchParams
    const target =
      `${EM}?pn=${pn}&pz=${pz}&po=0&np=1&fltt=2&invt=2&fid=f12` +
      `&ut=bd1d9ddb04089700cf9c27f6f7426281` +
      `&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048` +
      `&fields=f2,f3,f12,f14`;

    try {
      const r = await fetch(target, {
        headers: {
          "Referer": "https://quote.eastmoney.com/",
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
      });
      const body = await r.text();
      return new Response(body, {
        status: r.status,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
          "access-control-allow-origin": "*",
        },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e) }), {
        status: 502,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    }
  },
};
