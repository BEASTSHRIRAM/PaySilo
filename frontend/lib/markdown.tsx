import type { ReactNode } from "react";

/**
 * A tiny, safe markdown renderer for Silo's chat messages.
 * It builds React nodes only — no raw HTML is ever injected — and supports
 * paragraphs, bold, italic, inline code, bullet/numbered lists, small
 * headings and http(s) links. Anything else renders as plain text.
 */

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Split on **bold**, *italic*, `code` and [label](https://…) tokens.
  const pattern = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`\n]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
  const parts = text.split(pattern);
  parts.forEach((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (!part) return;
    if (part.startsWith("**") && part.endsWith("**")) {
      nodes.push(<strong key={key} className="font-extrabold text-sand-50">{part.slice(2, -2)}</strong>);
    } else if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      nodes.push(<em key={key}>{part.slice(1, -1)}</em>);
    } else if (part.startsWith("`") && part.endsWith("`")) {
      nodes.push(
        <code key={key} className="rounded-md bg-ink-950/70 px-1.5 py-0.5 font-mono text-[0.85em] text-sand-100">
          {part.slice(1, -1)}
        </code>,
      );
    } else if (part.startsWith("[")) {
      const match = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(part);
      if (match) {
        nodes.push(
          <a key={key} href={match[2]} target="_blank" rel="noreferrer" className="font-bold text-coral-300 underline underline-offset-2">
            {match[1]}
          </a>,
        );
      } else {
        nodes.push(part);
      }
    } else {
      nodes.push(part);
    }
  });
  return nodes;
}

export function MarkdownText({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="grid gap-2">
      {blocks.map((block, bi) => {
        const lines = block.split("\n").filter((l) => l.trim() !== "");
        if (lines.length === 0) return null;
        const heading = /^#{1,3}\s+(.*)$/.exec(lines[0]);
        if (heading && lines.length === 1) {
          return (
            <p key={bi} className="font-display text-[15px] font-semibold text-sand-50">
              {renderInline(heading[1], `h${bi}`)}
            </p>
          );
        }
        const isBullet = lines.every((l) => /^\s*[-•]\s+/.test(l));
        const isNumbered = lines.every((l) => /^\s*\d+[.)]\s+/.test(l));
        if (isBullet || isNumbered) {
          const Tag = isNumbered ? "ol" : "ul";
          return (
            <Tag key={bi} className={isNumbered ? "list-decimal space-y-1 pl-5" : "list-disc space-y-1 pl-5"}>
              {lines.map((line, li) => (
                <li key={li}>{renderInline(line.replace(/^\s*(?:[-•]|\d+[.)])\s+/, ""), `l${bi}-${li}`)}</li>
              ))}
            </Tag>
          );
        }
        return (
          <p key={bi} className="leading-relaxed">
            {lines.map((line, li) => (
              <span key={li}>
                {li > 0 && <br />}
                {renderInline(line, `p${bi}-${li}`)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
