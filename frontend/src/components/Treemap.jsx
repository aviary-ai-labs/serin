import React, { useMemo, useState } from 'react';
import { money } from '../format.js';

/**
 * Squarified treemap of holdings. Each tile is a position sized by market
 * value and tinted by sector. Beats a donut for portfolios with > ~8
 * positions because every holding gets a visible footprint.
 */
// Default viewBox height tuned for the squarified algorithm to produce
// rectangular tiles rather than horizontal bands at typical container widths.
export function AllocationTreemap({ positions = [], onSelect, height = 560 }) {
  const [hover, setHover] = useState(null);

  const items = useMemo(() => {
    return positions
      .filter(p => p.asset_type !== 'cash' && p.market_value > 0)
      .map(p => ({
        symbol: p.symbol,
        name: p.name || p.symbol,
        sector: p.sector || 'Unknown',
        value: p.market_value,
        position: p,
      }))
      .sort((a, b) => b.value - a.value);
  }, [positions]);

  const total = useMemo(() => items.reduce((sum, item) => sum + item.value, 0), [items]);

  // Closer-to-square aspect makes the squarified algorithm produce
  // proper 2D rectangles instead of full-width horizontal bands.
  const width = 800;
  const tiles = useMemo(() => squarify(items, total, width, height), [items, total, width, height]);

  if (!items.length) {
    return <div className="treemap-empty">No tracked holdings yet.</div>;
  }

  return (
    <div className="treemap">
      <svg viewBox={`0 0 ${width} ${height}`} className="treemap-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Holdings allocation treemap">
        {tiles.map(tile => {
          const isHovered = hover === tile.symbol;
          const pct = (tile.value / total) * 100;
          const fontSize = Math.min(18, Math.max(10, Math.sqrt(tile.w * tile.h) / 7));
          const showLabel = tile.w > 56 && tile.h > 28;
          const showPct = tile.w > 78 && tile.h > 50;
          return (
            <g
              key={tile.symbol}
              transform={`translate(${tile.x},${tile.y})`}
              className="treemap-tile"
              onMouseEnter={() => setHover(tile.symbol)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onSelect && onSelect(tile.position)}
              style={{ cursor: onSelect ? 'pointer' : 'default' }}
            >
              <rect
                width={tile.w}
                height={tile.h}
                fill={sectorColor(tile.sector)}
                stroke={isHovered ? 'rgba(255,255,255,0.9)' : 'rgba(15,22,18,0.7)'}
                strokeWidth={isHovered ? 1.5 : 1}
                rx={4}
              />
              {showLabel && (
                <text
                  x={8}
                  y={fontSize + 6}
                  fontSize={fontSize}
                  fontWeight="600"
                  fill="rgba(15,22,18,0.92)"
                >
                  {tile.symbol}
                </text>
              )}
              {showPct && (
                <text
                  x={8}
                  y={fontSize + 6 + fontSize + 4}
                  fontSize={fontSize - 2}
                  fill="rgba(15,22,18,0.7)"
                >
                  {pct.toFixed(1)}%
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hover && (
        <div className="treemap-tooltip" role="status">
          {(() => {
            const tile = tiles.find(t => t.symbol === hover);
            if (!tile) return null;
            return (
              <>
                <span className="treemap-tooltip-symbol">{tile.symbol}</span>
                <span className="treemap-tooltip-sector">{tile.sector}</span>
                <span className="treemap-tooltip-value">{money(tile.value)} · {((tile.value / total) * 100).toFixed(2)}%</span>
              </>
            );
          })()}
        </div>
      )}
    </div>
  );
}

// Squarified treemap algorithm (Bruls, Huijsing, van Wijk).
// Returns array of {symbol, name, sector, value, position, x, y, w, h}.
function squarify(items, total, width, height) {
  if (!items.length || total <= 0) return [];
  const tiles = [];
  let area = { x: 0, y: 0, w: width, h: height };
  const remaining = items.map(item => ({ ...item, normalized: (item.value / total) * (width * height) }));

  while (remaining.length) {
    const row = [];
    const shortSide = Math.min(area.w, area.h);
    let worst = Infinity;
    let i = 0;
    for (; i < remaining.length; i += 1) {
      row.push(remaining[i]);
      const next = worstRatio(row, shortSide);
      if (next > worst) {
        row.pop();
        break;
      }
      worst = next;
    }
    layoutRow(row, area, tiles);
    area = trimArea(area, row);
    remaining.splice(0, row.length);
  }
  return tiles;
}

function worstRatio(row, shortSide) {
  const sum = row.reduce((acc, item) => acc + item.normalized, 0);
  if (sum <= 0) return Infinity;
  const sq = shortSide * shortSide;
  const sumSq = sum * sum;
  let worst = 0;
  for (const item of row) {
    const ratio = Math.max((sq * item.normalized) / sumSq, sumSq / (sq * item.normalized));
    if (ratio > worst) worst = ratio;
  }
  return worst;
}

function layoutRow(row, area, tiles) {
  const sum = row.reduce((acc, item) => acc + item.normalized, 0);
  const horizontal = area.w >= area.h;
  if (horizontal) {
    const rowHeight = sum / area.w;
    let x = area.x;
    for (const item of row) {
      const tileWidth = item.normalized / rowHeight;
      tiles.push({ ...item, x, y: area.y, w: tileWidth, h: rowHeight });
      x += tileWidth;
    }
  } else {
    const rowWidth = sum / area.h;
    let y = area.y;
    for (const item of row) {
      const tileHeight = item.normalized / rowWidth;
      tiles.push({ ...item, x: area.x, y, w: rowWidth, h: tileHeight });
      y += tileHeight;
    }
  }
}

function trimArea(area, row) {
  const sum = row.reduce((acc, item) => acc + item.normalized, 0);
  const horizontal = area.w >= area.h;
  if (horizontal) {
    const rowHeight = sum / area.w;
    return { x: area.x, y: area.y + rowHeight, w: area.w, h: Math.max(0, area.h - rowHeight) };
  }
  const rowWidth = sum / area.h;
  return { x: area.x + rowWidth, y: area.y, w: Math.max(0, area.w - rowWidth), h: area.h };
}

const SECTOR_PALETTE = {
  'Technology': '#5fb39a',
  'Information Technology': '#5fb39a',
  'Financial Services': '#7c9fc7',
  'Financials': '#7c9fc7',
  'Health Care': '#c98b78',
  'Healthcare': '#c98b78',
  'Consumer Cyclical': '#d4a55a',
  'Consumer Discretionary': '#d4a55a',
  'Consumer Defensive': '#b6a479',
  'Consumer Staples': '#b6a479',
  'Communication Services': '#a78fbf',
  'Industrials': '#9fa97e',
  'Energy': '#cf9760',
  'Utilities': '#8ab0bd',
  'Real Estate': '#ba8a6b',
  'Basic Materials': '#a8856b',
  'Materials': '#a8856b',
  'ETF': '#90a39b',
  'Crypto': '#d8a25a',
  'Cash': '#b8c7be',
  'Unknown': '#9ba89f',
};

export function sectorColor(sector) {
  return SECTOR_PALETTE[sector] || hashColor(sector);
}

function hashColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i += 1) hash = (hash * 31 + str.charCodeAt(i)) | 0;
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 32%, 62%)`;
}
