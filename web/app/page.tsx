"use client";

import { buttonVariants } from "@/components/ui/button";
import { SIGN_IN_LABEL } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api.claven.app";

const ERROR_MESSAGES: Record<string, string> = {
  oauth_denied: "You declined the Gmail permission. Grant access to continue.",
  gmail_scope_missing: "Gmail access was not granted. Please sign in again and check the Gmail checkbox.",
  invalid_state: "The sign-in session expired. Please try again.",
  token_exchange_failed: "Could not complete sign-in. Please try again.",
  token_verification_failed: "Could not verify your account. Please try again.",
  signup_failed: "Sign-in succeeded but account setup failed. Please try again.",
  invalid_request: "Something went wrong. Please try again.",
};

function HomeContent() {
  const params = useSearchParams();
  const error = params.get("error");
  const errorDetail = params.get("error_detail");
  const errorMessage = error ? (ERROR_MESSAGES[error] ?? "Something went wrong. Please try again.") : null;

  return (
    <div className="flex flex-col items-center gap-6 text-center max-w-md">
      <h1 className="text-4xl font-bold tracking-tight">Claven</h1>
      <p className="text-lg text-muted-foreground leading-relaxed">
        Automatic Gmail labeling. Define your rules once — Claven applies them
        to every new email.
      </p>
      {errorMessage && (
        <div className="flex flex-col gap-1 text-sm text-destructive">
          <p>
            {errorMessage}
            {" "}
            <span className="font-mono opacity-60">[{error}]</span>
          </p>
          {errorDetail && (
            <p className="font-mono text-xs opacity-70 break-all">{errorDetail}</p>
          )}
        </div>
      )}
      <a
        href={`${API_URL}/oauth/start`}
        onClick={(e) => {
          e.preventDefault();
          window.location.href = `${API_URL}/oauth/start?return_to=${encodeURIComponent(window.location.origin)}`;
        }}
        className={cn(buttonVariants({ size: "lg" }), "mt-2")}
      >
        {errorMessage ? "Try again" : SIGN_IN_LABEL}
      </a>
      <a href="/how-it-works" className="text-sm text-muted-foreground hover:text-foreground mt-2">
        How it works
      </a>
      <nav className="flex gap-4 text-xs text-muted-foreground">
        <a href="/privacy" className="hover:text-foreground">Privacy</a>
        <a href="/terms" className="hover:text-foreground">Terms</a>
      </nav>
    </div>
  );
}

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
      <Suspense>
        <HomeContent />
      </Suspense>
    </main>
  );
}
