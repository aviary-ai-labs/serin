import React from 'react';
import { timeAgo } from '../format.js';

// A feed with a past needs a sense of when. Undated, thirty days of headlines
// read as one undifferentiated pile — the reader cannot tell yesterday from
// last week without inspecting each timestamp.
function dayLabel(published) {
  if (!published) return 'Earlier';
  const when = new Date(published);
  if (Number.isNaN(when.getTime())) return 'Earlier';
  const startOf = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(new Date()) - startOf(when)) / 86400000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return when.toLocaleDateString(undefined, { weekday: 'long' });
  return when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function groupByDay(items) {
  const groups = [];
  for (const item of items) {
    const label = dayLabel(item.published);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
}

function NewsList({ items, emptyText, showTicker = false }) {
  if (!items?.length) return <div className="empty-box">{emptyText}</div>;
  return (
    <div className="news-list">
      {groupByDay(items).map(group => (
        <React.Fragment key={group.label}>
          <div className="news-day">{group.label}</div>
          {group.items.map((item, idx) => (
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
        </React.Fragment>
      ))}
    </div>
  );
}

export function NewsView({ news, loading, onRefresh, onLoadMore, loadingMore }) {
  const retention = news?.retention_days;
  const canLoadMore = Boolean(news?.has_more && news?.next_before);

  return (
    <>
      <div className="news-toolbar">
        <span className="panel-note">
          {retention ? `A running feed — the last ${retention} days` : ''}
        </span>
        <button className="btn" onClick={onRefresh} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh news'}
        </button>
      </div>
      <div className="news-grid">
        <section className="panel">
          <div className="panel-header">
            <h2>Your Holdings</h2>
            <span className="panel-note">headlines naming your holdings</span>
          </div>
          {loading && !news ? (
            <div className="empty-box">Loading…</div>
          ) : (
            <NewsList
              items={news?.portfolio_news}
              emptyText="No headlines have mentioned your holdings recently."
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
          {canLoadMore && (
            <div className="news-more">
              <button className="btn btn-ghost btn-sm" onClick={onLoadMore} disabled={loadingMore}>
                {loadingMore ? 'Loading…' : 'Load older headlines'}
              </button>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
