import type { ReactNode } from 'react'

export type IconName =
  | 'warning'
  | 'info'
  | 'conflict'
  | 'calculator'
  | 'quote'
  | 'expand'
  | 'document'
  | 'shield'
  | 'send'
  | 'plus'
  | 'menu'
  | 'close'
  | 'copy'

const PATHS: Record<IconName, ReactNode> = {
  warning: (
    <>
      <path d="M10 3.2 17.4 16.4H2.6z" />
      <path d="M10 8.3v3.6M10 14v.01" />
    </>
  ),
  info: (
    <>
      <circle cx="10" cy="10" r="7.4" />
      <path d="M10 9.2v4.4M10 6.5v.01" />
    </>
  ),
  conflict: <path d="M3.5 6.5h11l-2.6-2.6M16.5 13.5h-11l2.6 2.6" />,
  calculator: (
    <>
      <rect x="4.5" y="2.8" width="11" height="14.4" rx="1.8" />
      <path d="M7.2 6h5.6M7.5 9.5v.01M10 9.5v.01M12.5 9.5v.01M7.5 12.5v.01M10 12.5v.01M12.5 12.5v.01" />
    </>
  ),
  quote: <path d="M4 7.5c0-1.7 1-2.8 2.6-3M4 7.5v4h3.4v-4zM11 7.5c0-1.7 1-2.8 2.6-3M11 7.5v4h3.4v-4z" />,
  expand: <path d="M12 3.5h4.5V8M8 16.5H3.5V12M16.5 3.5l-5 5M3.5 16.5l5-5" />,
  document: (
    <>
      <path d="M5.5 2.8h6l3 3v11.4h-9z" />
      <path d="M11.5 2.8v3h3M7.8 10h4.4M7.8 13h4.4" />
    </>
  ),
  shield: (
    <>
      <path d="M10 2.8 16 5.1v4.4c0 3.6-2.5 6.3-6 7.7-3.5-1.4-6-4.1-6-7.7V5.1z" />
      <path d="m7.4 10 1.8 1.8 3.4-3.6" />
    </>
  ),
  send: <path d="M3 17 17 3M17 3H8M17 3v9" />,
  plus: <path d="M10 4v12M4 10h12" />,
  menu: <path d="M2.5 5h15M2.5 10h15M2.5 15h15" />,
  close: <path d="m5 5 10 10M15 5 5 15" />,
  copy: (
    <>
      <rect x="7" y="7" width="9.5" height="9.5" rx="1.8" />
      <path d="M13 7V5.2A1.7 1.7 0 0 0 11.3 3.5H5.2A1.7 1.7 0 0 0 3.5 5.2v6.1A1.7 1.7 0 0 0 5.2 13H7" />
    </>
  ),
}

export function Icon({ name, size = 14 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  )
}
