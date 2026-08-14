// Cloudflare Worker：静态页面之外，提供两个东财代理接口。
//
// /api/quotes  实时行情快照代理 —— 前端 JSONP 直连失败时的兜底
// /api/kline   历史K线(前复权·等比)代理 —— 给 GitHub Actions 建库用：
//              东财封锁 GitHub 的 IP 段，但 GitHub 能访问 workers.dev，
//              而 Cloudflare 的出口 IP 未必在东财黑名单里，借道一试。
//
// public/ 目录下的静态资产会被平台优先匹配，命中不了的路径才进入这段代码。

const HEADERS = {
  "Referer": "https://quote.eastmoney.com/",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
};

function jsonResponse(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

async function relay(target) {
  try {
    const r = await fetch(target, { headers: HEADERS });
    return jsonResponse(await r.text(), r.status);
  } catch (e) {
    return jsonResponse(JSON.stringify({ error: String(e) }), 502);
  }
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/api/quotes") {
      const pn = parseInt(url.searchParams.get("pn") || "1", 10) || 1;
      const pz = Math.min(parseInt(url.searchParams.get("pz") || "100", 10) || 100, 1000);
      // fs 参数里的 "+" 需原样传给东财，故手动拼 URL
      const target =
        `https://push2.eastmoney.com/api/qt/clist/get?pn=${pn}&pz=${pz}&po=1&np=1&fltt=2&invt=2&fid=f12` +
        `&ut=bd1d9ddb04089700cf9c27f6f7426281` +
        `&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048` +
        `&fields=f2,f3,f12,f14`;
      return relay(target);
    }

    if (url.pathname === "/api/kline") {
      const secid = url.searchParams.get("secid") || "";
      if (!/^[01]\.\d{6}$/.test(secid)) {
        return jsonResponse(JSON.stringify({ error: "bad secid" }), 400);
      }
      const target =
        `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=${secid}` +
        `&klt=101&fqt=1&beg=19900101&end=20500101&lmt=1000000` +
        `&ut=7eea3edcaed734bea9cbfc24409ed989&fields1=f1,f2,f3&fields2=f51,f55`;
      return relay(target);
    }

    return new Response("Not found", { status: 404 });
  },
};
