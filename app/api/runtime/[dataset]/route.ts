import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const maxDuration = 30;

const RUNTIME_ROOT = 'https://raw.githubusercontent.com/theodore-song/polymarket-alpha-dashboard/runtime-state/runtime';
const DATASETS: Record<string, { file: string; contentType: string; attachment?: string }> = {
  dashboard: { file: 'dashboard.json', contentType: 'application/json; charset=utf-8' },
  trades: { file: 'trades.json', contentType: 'application/json; charset=utf-8' },
  equity: { file: 'equity.json', contentType: 'application/json; charset=utf-8' },
  health: { file: 'health.json', contentType: 'application/json; charset=utf-8' },
  ledger: { file: 'polyalpha-v2.sqlite3', contentType: 'application/vnd.sqlite3', attachment: 'polyalpha-v2.sqlite3' },
};

export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ dataset: string }> },
) {
  const { dataset } = await context.params;
  const selected = DATASETS[dataset];
  if (!selected) return Response.json({ error: 'Unknown runtime dataset' }, { status: 404 });

  const upstream = await fetch(`${RUNTIME_ROOT}/${selected.file}?runtime=${Date.now()}`, {
    cache: 'no-store',
    headers: { Accept: selected.contentType, 'Cache-Control': 'no-cache' },
  });
  if (!upstream.ok) {
    return Response.json(
      { error: 'Live runtime data is not available yet', upstream_status: upstream.status },
      { status: 503, headers: { 'Cache-Control': 'no-store, max-age=0' } },
    );
  }

  const headers = new Headers({
    'Content-Type': selected.contentType,
    'Cache-Control': 'no-store, max-age=0, must-revalidate',
    'CDN-Cache-Control': 'no-store',
    'Vercel-CDN-Cache-Control': 'no-store',
  });
  if (selected.attachment) {
    headers.set('Content-Disposition', `attachment; filename="${selected.attachment}"`);
  }
  return new Response(upstream.body, { status: 200, headers });
}
