import React from 'react';
import { timeAgo } from '../format.js';

function NewsList({ items, emptyText, showTicker = false }) {
  if (!items?.length) return <div className="empty-box">{emptyText}</div>;
  return (
    <div className="news-list">
      {items.map((item, idx) => (
        <a className="news-item" key={`${item.link}-${idx}`} href={item.link} target="_blank" rel="noreferrer">
          <span className="news-item-meta">
            {showTicker && item.matched_ticker && <span className="ticker-chip">{item.matched_ticker}</span>}
            <span>{item.source}</span>
            {item.published && <span>{timeAgo(item.published)}</span>}
          </span>
          <strong>{item.title}</strong>
          {item.summary && <p>{item.summary}</p>}
        </a>
      ))}
    </div>
  );
}

export function NewsView({ news, loading, onRefresh }) {
  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh news'}
        </button>
      </div>
      <div className="news-grid">
        <section className="panel">
          <div className="panel-header">
            <h2>Your Holdings</h2>
            <span className="panel-note">headlines matching your tickers</span>
          </div>
          {loading && !news ? (
            <div className="empty-box">Loading…</div>
          ) : (
            <NewsList
              items={news?.portfolio_news}
              emptyText="No headlines mention your holdings right now."
              showTicker
            />
          )}
        </section>
        <section className="panel">
          <div className="panel-header">
            <h2>Market</h2>
            <span className="panel-note">MarketWatch · CNBC</span>
          </div>
          {loading && !news ? (
            <div className="empty-box">Loading…</div>
          ) : (
            <NewsList items={news?.market_news} emptyText="No market headlines available." />
          )}
        </section>
      </div>
    </>
  );
}
