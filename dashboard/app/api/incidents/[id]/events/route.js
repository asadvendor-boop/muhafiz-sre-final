// SSE Proxy — Streams events from FastAPI gateway without buffering
// Next.js rewrites buffer SSE responses, so we need a proper Route Handler

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request, { params }) {
  const { id } = await params;
  const searchParams = request.nextUrl?.searchParams;
  const lastEventId = searchParams?.get("last_event_id") || "";

  const gatewayUrl =
    process.env.GATEWAY_INTERNAL_URL || "http://localhost:8000";
  let url = `${gatewayUrl}/api/incidents/${id}/events`;
  if (lastEventId) {
    url += `?last_event_id=${encodeURIComponent(lastEventId)}`;
  }

  try {
    const upstreamRes = await fetch(url, {
      headers: {
        Accept: "text/event-stream",
        "Cache-Control": "no-cache",
      },
      signal: request.signal,
    });

    if (!upstreamRes.ok) {
      return new Response(`Gateway error: ${upstreamRes.status}`, {
        status: upstreamRes.status,
      });
    }

    // Stream the SSE response directly without buffering
    return new Response(upstreamRes.body, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (err) {
    console.error("[SSE Proxy] Error:", err.message);
    return new Response(`SSE proxy error: ${err.message}`, { status: 502 });
  }
}
