import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { ThemeToggle } from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "CodeAtlas",
  description: "Ask questions about a codebase and get answers with exact file and line citations.",
};

// Set the theme class before paint to avoid a flash (explicit choice > OS preference).
const themeScript = `(function(){try{var t=localStorage.getItem('theme');var d=t?t==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;if(d)document.documentElement.classList.add('dark');}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-screen antialiased">
        <header className="sticky top-0 z-20 border-b border-line bg-card/90 backdrop-blur">
          <nav className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3" aria-label="Main">
            <Link href="/" className="font-semibold tracking-tight">
              CodeAtlas
            </Link>
            <Link href="/" className="text-sm text-muted hover:text-fg">
              Repositories
            </Link>
            <Link href="/results" className="text-sm text-muted hover:text-fg">
              Results
            </Link>
            <div className="ml-auto">
              <ThemeToggle />
            </div>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
