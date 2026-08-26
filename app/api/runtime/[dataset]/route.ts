import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const maxDuration = 30;

const REPOSITORY = 'theodore-song/polymarket-alpha-dashboard';
const RUNTIME_REF = 'refs/heads/runtime-state';
const RUNTIME_REFS_URL = `https://github.com/${REPOSITORY}.git/info/refs?service=git-upload-pack`;
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

  // Resolve the branch head first, then fetch by immutable commit. GitHub's raw
  // branch URLs can remain stale at an individual CDN point after a force-push.
  const refs = await fetch(RUNTIME_REFS_URL, {
    cache: 'no-store',
    headers: { Accept: 'application/x-git-upload-pack-advertisement', 'Cache-Control': 'no-cache' },
  });
  const advertisedRefs = refs.ok ? await refs.text() : '';
  const escapedRef = RUNTIME_REF.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const runtimeCommit = advertisedRefs.match(new RegExp(`([0-9a-f]{40}) ${escapedRef}(?:\\0|\\n|$)`))?.[1];
  if (!runtimeCommit) {
    return Response.json(
      { error: 'Live runtime state is not available yet', upstream_status: refs.status },
      { status: 503, headers: { 'Cache-Control': 'no-store, max-age=0' } },
    );
  }

  const upstream = await fetch(
    `https://raw.githubusercontent.com/${REPOSITORY}/${runtimeCommit}/runtime/${selected.file}`,
    { cache: 'force-cache', headers: { Accept: selected.contentType } },
  );
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
    'X-PolyAlpha-Runtime-Commit': runtimeCommit,
  });
  if (selected.attachment) {
    headers.set('Content-Disposition', `attachment; filename="${selected.attachment}"`);
  }
  return new Response(upstream.body, { status: 200, headers });
}
