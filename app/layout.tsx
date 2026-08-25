import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL
  ?? (process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` : 'http://localhost:3000');

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: 'PolyAlpha — 100 Agent Paper Ledger',
  description: 'Explore every trade, position, order, and portfolio across 100 Polymarket paper-trading agents.',
  openGraph: {
    title: 'PolyAlpha — 100 Agent Paper Ledger',
    description: 'Every paper trade, position, resting order, and virtual portfolio across 100 Polymarket agents.',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: 'PolyAlpha 100 Agent Paper Ledger' }],
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'PolyAlpha — 100 Agent Paper Ledger',
    description: 'Every paper trade, position, resting order, and virtual portfolio across 100 Polymarket agents.',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
