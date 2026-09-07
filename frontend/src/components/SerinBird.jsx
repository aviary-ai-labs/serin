import React from 'react';

/** Serin's yellow-and-olive companion mark. */
export function SerinBird({ className = '', title = '' }) {
  return (
    <svg
      className={`serin-bird ${className}`.trim()}
      viewBox="0 0 88 60"
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : 'true'}
      aria-label={title || undefined}
    >
      <path className="bird-tail" d="m59 35 24.8 13.8c2.5 1.4 2.2 4.6-.4 5.4-1 .3-2 .1-2.8-.4L54 43.2Z" />
      <path className="bird-body" d="M20.9 27.1c-3-4.3-2.7-10.2.1-15 4.1-7.1 13.5-9.7 21.1-6 7 3.4 10.6 11 8.5 18.2 7.9.7 14.2 4.7 17.2 10.4 2.8 5.3.1 11.4-5.8 14.4-8.7 4.5-22.1 3.3-31.4-2.5-7.7-4.8-12-12.6-9.7-19.5Z" />
      <path className="bird-highlight" d="M25.2 12.8c3.4-5 10.5-7.2 16-4.8-6.3.3-11.4 3.3-15.2 9-1.6 2.3-2.8-1.3-.8-4.2Z" />
      <path className="bird-beak" d="m21.3 18.1-12.9 6 12.7 6.4 3.6-6.3Z" />
      <path className="bird-wing" d="M43.6 25.1c8.7-.1 16.5 4.8 21.4 13.2-4.7 5.1-10.9 7.4-17.2 5.7-6.8-1.9-11.1-7-11-12 .1-4.2 2.8-6.8 6.8-6.9Z" />
      <circle className="bird-eye" cx="30.5" cy="17.2" r="2.6" />
      <path className="bird-stroke" d="m59 35 24.8 13.8c2.5 1.4 2.2 4.6-.4 5.4-1 .3-2 .1-2.8-.4l-19-7.6M20.9 27.1c-3-4.3-2.7-10.2.1-15 4.1-7.1 13.5-9.7 21.1-6 7 3.4 10.6 11 8.5 18.2 7.9.7 14.2 4.7 17.2 10.4 2.8 5.3.1 11.4-5.8 14.4-8.7 4.5-22.1 3.3-31.4-2.5-7.7-4.8-12-12.6-9.7-19.5Z" />
      <path className="bird-stroke" d="m21.3 18.1-12.9 6 12.7 6.4M43.6 25.1c8.7-.1 16.5 4.8 21.4 13.2-4.7 5.1-10.9 7.4-17.2 5.7-6.8-1.9-11.1-7-11-12 .1-4.2 2.8-6.8 6.8-6.9ZM39.4 50.3l-1.1 6.1m13.9-5.1 2 5.1m-20.4 0h8.7m7.4 0h8.4" />
    </svg>
  );
}
