"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        h1: ({ children }) => <h1 className="text-3xl font-bold tracking-tight mb-6">{children}</h1>,
        h2: ({ children }) => <h2 className="text-xl font-semibold mt-8 mb-3">{children}</h2>,
        h3: ({ children }) => <h3 className="text-base font-semibold mt-6 mb-2">{children}</h3>,
        p: ({ children }) => <p className="text-sm text-muted-foreground mb-3 leading-relaxed">{children}</p>,
        ul: ({ children }) => <ul className="text-sm text-muted-foreground mb-3 ml-4 list-disc space-y-1">{children}</ul>,
        li: ({ children }) => <li className="leading-relaxed">{children}</li>,
        code: ({ children }) => <code className="text-xs bg-muted px-1.5 py-0.5 rounded font-mono">{children}</code>,
        a: ({ href, children }) => <a href={href} className="underline underline-offset-2 hover:text-foreground">{children}</a>,
        strong: ({ children }) => <strong className="text-foreground font-medium">{children}</strong>,
        table: ({ children }) => <div className="overflow-x-auto mb-3"><table className="w-full text-sm border-collapse">{children}</table></div>,
        thead: ({ children }) => <thead className="border-b border-border">{children}</thead>,
        th: ({ children }) => <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-4">{children}</th>,
        td: ({ children }) => <td className="text-sm text-muted-foreground py-2 pr-4 align-top">{children}</td>,
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
