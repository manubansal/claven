import { MarkdownContent } from "@/components/markdown-content";
import md from "./content";

export default function HowItWorksPage() {
  return (
    <main className="flex flex-1 flex-col items-center px-6 py-16">
      <article className="w-full max-w-2xl">
        <MarkdownContent content={md} />
      </article>
    </main>
  );
}
