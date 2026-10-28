"use client";

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog";
import { buttonVariants } from "@/components/ui/button";
import { SIGN_IN_LABEL } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { AlertCircle, AlertTriangle, CheckCircle, Clock, Loader2, RefreshCw, Zap } from "lucide-react";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api.claven.app";

type MeResponse = {
  email: string;
  connected: boolean;
  history_id: number | null;
  known_senders: number;
  pending_relabel_count: number;
  sent_scanned_count: number;
  sent_total_count: number | null;
  sent_scan_status: string | null;
  sent_scan_health: { code: string; label: string; severity: string } | null;
  inbox_scan_status: string | null;
  scan_health: { code: string; label: string; severity: string } | null;
  last_fetched_at: string | null;
  last_labeled_at: string | null;
  newest_mail_at: string | null;
  newest_labeled_at: string | null;
  unread_count: number | null;
  read_count: number | null;
  inbox_count: number | null;
  all_mail_count: number | null;
  allmail_labeled_known_count: number | null;
  allmail_labeled_unknown_count: number | null;
  allmail_labeled_total_count: number | null;
  inbox_unlabeled_first_page_count: number | null;
  inbox_unlabeled_deep_count: number | null;
  allmail_unlabeled_first_page_count: number | null;
  inbox_labeled_known_shallow_count: number | null;
  inbox_labeled_known_has_more: boolean | null;
  inbox_labeled_unknown_shallow_count: number | null;
  inbox_labeled_unknown_has_more: boolean | null;
  archive_job: { job_id: string; status: string; total: number | null; progress: number | null } | null;
  recent_events: { timestamp: string; event_type: string; message: string }[];
  reset_sent_job: { job_id: string; status: string; total: number | null; progress: number | null } | null;
  scan_scope: "inbox" | "allmail" | null;
  auto_archive_unknown: boolean;
  inbox_mode?: InboxMode;
  sunset_hours?: number;
  cancel_state: string | null;
  gmail_error: { code: string; label: string; severity: string } | null;
};

type InboxMode = "label_only" | "gatekeeper" | "sunset";

const INBOX_MODES: { value: InboxMode; label: string; hint: string }[] = [
  { value: "label_only", label: "Label only", hint: "Label mail; never archive anything" },
  { value: "gatekeeper", label: "Gatekeeper", hint: "Archive unknown-sender mail as soon as it arrives" },
  { value: "sunset", label: "Sunset", hint: "Unknown-sender mail stays in the inbox; archived after the timer unless read or starred" },
];

const SUNSET_HOURS: { value: number; label: string }[] = [
  { value: 4, label: "4h" },
  { value: 12, label: "12h" },
  { value: 24, label: "24h" },
  { value: 72, label: "3 days" },
];

type LabelRule = {
  field: string;
  known_sender?: boolean;
  contains?: string[];
};

type LabelConfig = {
  id: string;
  name: string;
  description?: string;
  unknown_label?: string;
  rules: LabelRule[];
};

type State =
  | { status: "loading" }
  | { status: "unauthenticated" }
  | { status: "loaded"; data: MeResponse; labels: LabelConfig[] };

function ResetSentButton({ disabled, onConfirm }: { disabled: boolean; onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger
        disabled={disabled}
        className="text-[10px] px-2 py-0.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-50 transition-colors"
      >
        {disabled ? "Starting…" : "Reset"}
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Reset sent scan</AlertDialogTitle>
          <AlertDialogDescription>
            This will remove all sent-scanned labels and re-scan your entire Sent mail. Your known senders list will be rebuilt from scratch. This can take a while for large mailboxes.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <button
            className={cn(buttonVariants())}
            onClick={() => { setOpen(false); onConfirm(); }}
          >
            Reset sent scan
          </button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ArchiveButton({ label, disabled, onConfirm }: { label: string; disabled: boolean; onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger
        disabled={disabled}
        className="text-[10px] px-2 py-0.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-50 transition-colors"
      >
        {label}
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Archive unknown-sender messages</AlertDialogTitle>
          <AlertDialogDescription>
            This will remove all unknown-sender messages from your inbox. The messages won&apos;t be deleted — they&apos;ll still be in All Mail. This action can take a while for large counts.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <button
            className={cn(buttonVariants())}
            onClick={() => { setOpen(false); onConfirm(); }}
          >
            {label}
          </button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function InfoSection({
  icon: Icon,
  iconElement,
  iconColor = "",
  iconSpin = false,
  iconTestId,
  title,
  errorCode,
  action,
  rows,
}: {
  icon?: typeof Clock;
  iconElement?: ReactNode;
  iconColor?: string;
  iconSpin?: boolean;
  iconTestId?: string;
  title: string;
  errorCode?: string | null;
  action?: ReactNode;
  rows: { label: ReactNode; value: ReactNode }[];
}) {
  return (
    <div className="flex flex-col gap-2 mt-5 pt-3 border-t border-border/30">
      <div className="flex items-center justify-between">
        <span className="inline-flex items-baseline gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/60">
          {iconElement ?? (Icon && <Icon className={cn("h-3 w-3 self-center", iconColor, iconSpin && "animate-spin")} data-testid={iconTestId} />)}
          <span>{title}</span>
          {errorCode && (
            <span
              className={cn("cursor-pointer hover:underline normal-case tracking-normal", iconColor || "text-muted-foreground")}
              title={`${errorCode} — click to copy`}
              onClick={() => navigator.clipboard.writeText(errorCode)}
            >{errorCode}</span>
          )}
        </span>
        {action}
      </div>
      <div className="flex flex-col gap-0.5">
        {rows.map((row, i) => (
          <div key={i} className="flex justify-between gap-4 items-center">
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">{row.label}</span>
            <span className="text-xs tabular-nums text-muted-foreground">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const [state, setState] = useState<State>({ status: "loading" });
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [topSenders, setTopSenders] = useState<{ email: string; count: number }[]>([]);
  const [topDomains, setTopDomains] = useState<{ domain: string; count: number }[]>([]);
  const [topSendersLoading, setTopSendersLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(id);
  }, []);

  async function loadData() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      const [meRes, configRes] = await Promise.all([
        fetch(`${API_URL}/api/me`, { credentials: "include", signal: controller.signal }),
        fetch(`${API_URL}/api/config`, { signal: controller.signal }),
      ]);
      if (meRes.status === 401) {
        setState({ status: "unauthenticated" });
        return;
      }
      if (!meRes.ok) {
        // Server error — keep stale data if available, otherwise show sign-in
        if (state.status === "loading") setState({ status: "unauthenticated" });
        return;
      }
      const [data, config] = await Promise.all([meRes.json(), configRes.json()]);
      // Fetch top senders separately — non-blocking, doesn't affect auth flow
      setTopSendersLoading(true);
      fetch(`${API_URL}/api/top-senders`, { credentials: "include" })
        .then(async (res) => {
          if (res.ok) {
            const topData = await res.json();
            setTopSenders(topData.top_senders ?? []);
            setTopDomains(topData.top_domains ?? []);
          }
        })
        .catch(() => {})
        .finally(() => setTopSendersLoading(false));
      setState({ status: "loaded", data, labels: config.labels ?? [] });
      setLastUpdated(new Date());
    } catch {
      // Network error or timeout — keep stale data if available
      if (state.status === "loading") setState({ status: "unauthenticated" });
    } finally {
      clearTimeout(timeout);
    }
  }

  useEffect(() => { loadData(); }, []);

  // Auto-refresh when tab regains focus (e.g., returning after hours)
  useEffect(() => {
    const onFocus = () => { loadData(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  // Live progress via Server-Sent Events — workers push updates after each
  // batch commit via Postgres NOTIFY. No polling, no wasted API calls.
  // Debounced: rapid events (multiple batches/second) collapse into one
  // loadData call every 3 seconds to avoid flooding /api/me.
  useEffect(() => {
    if (state.status !== "loaded") return;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    const es = new EventSource(`${API_URL}/api/events`, { withCredentials: true });
    es.onmessage = () => {
      if (!debounceTimer) {
        debounceTimer = setTimeout(() => {
          debounceTimer = null;
          loadData();
        }, 3_000);
      }
    };
    es.onerror = () => {
      // SSE disconnected — EventSource auto-reconnects (browser default)
    };
    return () => {
      es.close();
      if (debounceTimer) clearTimeout(debounceTimer);
    };
  }, [state.status]);

  function formatRelativeTime(date: Date, suffix = "ago"): string {
    const seconds = Math.floor((now - date.getTime()) / 1000);
    if (seconds < 60) return seconds <= 5 && suffix === "ago" ? "just now" : `${seconds} seconds ${suffix}`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes === 1 ? `1 minute ${suffix}` : `${minutes} minutes ${suffix}`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours === 1 ? `1 hour ${suffix}` : `${hours} hours ${suffix}`;
    const days = Math.floor(hours / 24);
    return days === 1 ? `1 day ${suffix}` : `${days} days ${suffix}`;
  }

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await loadData();
    } finally {
      setRefreshing(false);
    }
  }

  async function handleLogout() {
    setLoggingOut(true);
    await fetch(`${API_URL}/api/logout`, { method: "POST", credentials: "include" });
    router.replace("/");
  }

  async function handleSwitchAccount() {
    await fetch(`${API_URL}/api/logout`, { method: "POST", credentials: "include" });
    window.location.href = `${API_URL}/oauth/start?return_to=${encodeURIComponent(window.location.origin)}`;
  }

  async function handleConnect() {
    setConnecting(true);
    const res = await fetch(`${API_URL}/api/connect`, { method: "POST", credentials: "include" });
    if (res.ok) {
      await loadData();
    }
    setConnecting(false);
  }

  async function handleDisconnect() {
    setDisconnecting(true);
    await fetch(`${API_URL}/api/disconnect`, { method: "POST", credentials: "include" });
    setState((prev) =>
      prev.status === "loaded"
        ? { ...prev, data: { ...prev.data, connected: false, history_id: null } }
        : prev,
    );
    setDisconnecting(false);
  }

  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  function handleArchiveUnknown() {
    setArchiving(true);
    setCancelling(false);
    setArchiveError(null);
    fetch(`${API_URL}/api/actions/archive-unknown`, { method: "POST", credentials: "include" })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setArchiveError(body.detail || "Failed to start archive");
        }
      })
      .catch(() => setArchiveError("Network error"))
      .finally(() => { setArchiving(false); loadData(); });
  }

  function handleScanScope(scope: "inbox" | "allmail") {
    setState((prev) =>
      prev.status === "loaded"
        ? { ...prev, data: { ...prev.data, scan_scope: scope } }
        : prev,
    );
    fetch(`${API_URL}/api/settings/scan-scope`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope }),
    })
      .then((res) => { loadData(); })
      .catch(() => loadData());
  }

  function handleInboxMode(mode: InboxMode, sunsetHours?: number) {
    setState((prev) =>
      prev.status === "loaded"
        ? {
            ...prev,
            data: {
              ...prev.data,
              inbox_mode: mode,
              auto_archive_unknown: mode === "gatekeeper",
              ...(sunsetHours !== undefined ? { sunset_hours: sunsetHours } : {}),
            },
          }
        : prev,
    );
    fetch(`${API_URL}/api/settings/inbox-mode`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sunsetHours !== undefined ? { mode, sunset_hours: sunsetHours } : { mode }),
    })
      .then(() => { loadData(); })
      .catch(() => loadData());
  }

  const [resettingSent, setResettingSent] = useState(false);

  function handleResetSentScan() {
    setResettingSent(true);
    fetch(`${API_URL}/api/actions/reset-sent-scan`, { method: "POST", credentials: "include" })
      .finally(() => { setResettingSent(false); loadData(); });
  }

  async function handleCancelAction() {
    setCancelling(true);
    fetch(`${API_URL}/api/actions/cancel`, { method: "POST", credentials: "include" });
  }

  if (state.status === "loading") {
    return (
      <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
        <p className="text-muted-foreground">Loading…</p>
      </main>
    );
  }

  if (state.status === "unauthenticated") {
    return (
      <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
        <div className="flex flex-col items-center gap-6 text-center max-w-md">
          <p className="text-muted-foreground">You are not signed in.</p>
          <a
            href={`${API_URL}/oauth/start`}
            onClick={(e) => {
              e.preventDefault();
              window.location.href = `${API_URL}/oauth/start?return_to=${encodeURIComponent(window.location.origin)}`;
            }}
            className={cn(buttonVariants())}
          >
            {SIGN_IN_LABEL}
          </a>
        </div>
      </main>
    );
  }

  const { email, connected, known_senders, sent_scanned_count, sent_total_count, sent_scan_status, inbox_scan_status, scan_health, unread_count, read_count, inbox_count, all_mail_count, allmail_labeled_known_count, allmail_labeled_unknown_count, allmail_labeled_total_count, inbox_unlabeled_first_page_count, inbox_unlabeled_deep_count, inbox_labeled_known_shallow_count, inbox_labeled_known_has_more, inbox_labeled_unknown_shallow_count, inbox_labeled_unknown_has_more, archive_job, scan_scope } = state.data;
  const { labels } = state;

  const archiveCount = inbox_labeled_unknown_shallow_count ?? 0;
  const archiveHasMore = inbox_labeled_unknown_has_more ?? false;
  const archiveLabel = archiveCount === 0
    ? "Archive unknown-sender"
    : archiveHasMore
      ? `Archive ${archiveCount}+ unknown-sender`
      : `Archive ${archiveCount} unknown-sender`;
  const archiveRunning = archive_job?.status === "in_progress";

  return (
    <>
      <header className="flex items-center justify-end gap-2 border-b px-6 py-3">
        <a href="/how-it-works" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}>
          How it works
        </a>
        {connected && (
          <button
            onClick={handleDisconnect}
            disabled={disconnecting}
            className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
          >
            {disconnecting ? "Disconnecting…" : "Disconnect"}
          </button>
        )}
        <button
          onClick={handleSwitchAccount}
          className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
        >
          Switch account
        </button>
        <button
          onClick={handleLogout}
          disabled={loggingOut}
          className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
        >
          {loggingOut ? "Logging out…" : "Log out"}
        </button>
      </header>
      <main className="flex flex-1 flex-col items-center justify-center px-6 py-24">
        <div className="flex flex-col items-center gap-6 text-center w-full max-w-md">
          <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>

          <p className="text-lg font-medium">{email}</p>

          <div className="flex items-center gap-2">
            {connected ? (
              <>
                <CheckCircle className="h-5 w-5 text-green-500" />
                <span className="text-sm text-muted-foreground">Connected</span>
              </>
            ) : (
              <>
                <Zap className="h-5 w-5 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Connected and ready to start labeling</span>
              </>
            )}
          </div>

          <div className="w-full flex flex-col gap-1">
            <div className="w-full rounded-lg border bg-muted/40 px-5 py-4 text-sm divide-y divide-border/50">
              <div className="flex justify-between gap-4 py-3 first:pt-0 last:pb-0">
                <span className="text-muted-foreground">Inbox</span>
                <span className="tabular-nums">{inbox_count ?? "—"}</span>
              </div>
              <div className="flex justify-between gap-4 py-3 first:pt-0 last:pb-0">
                <span className="text-muted-foreground">Read</span>
                <span className="tabular-nums">{read_count ?? "—"}</span>
              </div>
              <div className="flex justify-between gap-4 py-3 first:pt-0 last:pb-0">
                <span className="text-muted-foreground">Unread</span>
                <span className="tabular-nums">{unread_count ?? "—"}</span>
              </div>
              <div className="flex justify-between gap-4 py-3 first:pt-0 last:pb-0">
                <span className="text-muted-foreground">All mail</span>
                <span className="tabular-nums">{all_mail_count ?? "—"}</span>
              </div>
              {labels.map((label) => {
                const isKnownSender = label.rules.some((r) => r.known_sender);
                const desc = label.description ?? label.rules
                  .map((r) =>
                    r.known_sender
                      ? `${r.field} is a known sender`
                      : `${r.field} contains ${r.contains?.join(", ")}`,
                  )
                  .join("; ");
                return (
                  <div key={label.id} className="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">
                    <span className="font-medium">{label.name}</span>
                    <span className="text-xs text-muted-foreground">{desc}</span>
                    {isKnownSender && (
                      <div className="flex flex-col items-center gap-2 mt-3">
                        <div className="flex rounded-md border border-border/50 overflow-hidden text-[10px] text-muted-foreground w-36">
                          <button
                            onClick={() => { if (!connected) handleConnect(); }}
                            disabled={connecting || disconnecting}
                            className={cn(
                              "flex-1 px-3 py-0.5 transition-colors text-center",
                              connected
                                ? "bg-muted font-medium text-foreground"
                                : "hover:text-foreground"
                            )}
                          >
                            {connecting ? "Starting…" : "Active"}
                          </button>
                          <button
                            onClick={() => { if (connected) handleDisconnect(); }}
                            disabled={connecting || disconnecting}
                            className={cn(
                              "flex-1 px-3 py-0.5 transition-colors border-l border-border/50 text-center",
                              !connected
                                ? "bg-muted font-medium text-foreground"
                                : "hover:text-foreground"
                            )}
                          >
                            {disconnecting ? "Pausing…" : "Inactive"}
                          </button>
                        </div>
                      </div>
                    )}
                    {isKnownSender && (() => {
                      const sentIsError = sent_scan_status?.startsWith("error");
                      const resetJob = state.data.reset_sent_job;
                      return (
                        <InfoSection
                          icon={sent_scan_status === "in_progress" ? Loader2 : sent_scan_status === "complete" ? CheckCircle : sentIsError ? AlertCircle : sent_scan_status === "cancelled" ? AlertTriangle : Clock}
                          iconColor={sent_scan_status === "complete" ? "text-green-500" : sentIsError ? "text-destructive" : sent_scan_status === "cancelled" ? "text-yellow-500" : ""}
                          iconSpin={sent_scan_status === "in_progress"}
                          iconTestId="sent-scan-icon"
                          title="Sent scan"
                          errorCode={sent_scan_status !== "in_progress" && sent_scan_status !== "complete" ? (state.data.sent_scan_health?.code ?? null) : null}
                          action={resetJob?.status === "in_progress" ? (
                            <button onClick={handleCancelAction} disabled={cancelling} className="text-[10px] text-muted-foreground hover:text-foreground">
                              {cancelling ? "Cancelling…" : `Resetting ${resetJob.progress ?? 0}/${resetJob.total ?? "…"} — cancel`}
                            </button>
                          ) : (
                            <ResetSentButton disabled={resettingSent} onConfirm={handleResetSentScan} />
                          )}
                          rows={[
                            { label: "Messages scanned", value: `${sent_scanned_count}${sent_total_count !== null ? ` / ${sent_total_count}` : ""}` },
                            { label: "Known senders found", value: known_senders },
                          ]}
                        />
                      );
                    })()}
                    {isKnownSender && (
                      <InfoSection
                        icon={state.data.pending_relabel_count > 0 ? Clock : CheckCircle}
                        iconColor={state.data.pending_relabel_count > 0 ? "" : "text-green-500"}
                        title="Relabel scan"
                        rows={[
                          { label: "Pending senders", value: state.data.pending_relabel_count },
                        ]}
                      />
                    )}
                    {label.unknown_label !== undefined && (() => {
                      const severity = scan_health?.severity;
                      const inboxErrorCode = (severity === "error" || severity === "warning") ? scan_health?.code : null;
                      const iconTestId = severity === "info" ? "filter-labeling-icon"
                        : severity === "error" ? "filter-error-icon"
                        : severity === "warning" ? "filter-warning-icon"
                        : severity === "success" ? "filter-complete-icon"
                        : "filter-waiting-icon";
                      const inboxMode: InboxMode = state.data.inbox_mode
                        ?? (state.data.auto_archive_unknown ? "gatekeeper" : "label_only");
                      const unknownCountLabel = (
                        <>
                          Inbox unknown-sender
                          {archiveRunning ? (
                            <button onClick={handleCancelAction} disabled={cancelling}
                              className="text-[10px] px-2 py-0.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-50 transition-colors">
                              {cancelling ? "Cancelling…" : `Archiving ${archive_job?.progress ?? 0}/${archive_job?.total ?? "…"} — cancel`}
                            </button>
                          ) : (
                            <ArchiveButton label={archiving ? "Starting…" : "Archive"}
                              disabled={!connected || archiveCount === 0 || archiving} onConfirm={handleArchiveUnknown} />
                          )}
                        </>
                      );
                      return (
                        <>
                          <InfoSection
                            title="Inbox mode"
                            action={
                              <div className="flex gap-2">
                                <div className="flex rounded-md border border-border/50 overflow-hidden text-[10px] text-muted-foreground">
                                  {INBOX_MODES.map((m, i) => {
                                    const active = inboxMode === m.value;
                                    return (
                                      <button key={m.value} title={m.hint} aria-pressed={active}
                                        onClick={() => { if (!active) handleInboxMode(m.value); }}
                                        className={cn("px-2 py-0.5 transition-colors text-center whitespace-nowrap",
                                          i > 0 && "border-l border-border/50",
                                          active ? "bg-muted font-medium text-foreground" : "hover:text-foreground")}>
                                        {m.label}
                                      </button>
                                    );
                                  })}
                                </div>
                                {inboxMode === "sunset" && (
                                  <select aria-label="Sunset timer" value={String(state.data.sunset_hours ?? 24)}
                                    onChange={(e) => handleInboxMode("sunset", Number(e.target.value))}
                                    className="text-[10px] rounded-md border border-border/50 bg-background px-1 py-0.5 text-muted-foreground">
                                    {SUNSET_HOURS.map((h) => (
                                      <option key={h.value} value={h.value}>{h.label}</option>
                                    ))}
                                  </select>
                                )}
                              </div>
                            }
                            rows={[
                              { label: <span className="text-left">{INBOX_MODES.find((m) => m.value === inboxMode)?.hint}</span>, value: null },
                            ]}
                          />
                          <InfoSection
                            icon={severity === "info" ? Loader2 : severity === "warning" ? AlertTriangle : severity === "error" ? AlertCircle : severity === "success" ? CheckCircle : Clock}
                            iconColor={severity === "error" ? "text-destructive" : severity === "warning" ? "text-yellow-500" : severity === "success" ? "text-green-500" : ""}
                            iconSpin={severity === "info"}
                            iconTestId={iconTestId}
                            title="Inbox scan"
                            errorCode={inboxErrorCode}
                            action={
                              <div className="flex gap-2">
                                <div className="flex rounded-md border border-border/50 overflow-hidden text-[10px] text-muted-foreground w-36">
                                  <button onClick={() => handleScanScope("inbox")}
                                    className={cn("flex-1 px-3 py-0.5 transition-colors text-center",
                                      (scan_scope ?? "inbox") === "inbox" ? "bg-muted font-medium text-foreground" : "hover:text-foreground")}>
                                    Inbox
                                  </button>
                                  <button onClick={() => handleScanScope("allmail")}
                                    className={cn("flex-1 px-3 py-0.5 transition-colors border-l border-border/50 text-center",
                                      scan_scope === "allmail" ? "bg-muted font-medium text-foreground" : "hover:text-foreground")}>
                                    All mail
                                  </button>
                                </div>
                              </div>
                            }
                            rows={[
                              { label: "Allmail labeled", value: allmail_labeled_total_count ?? "—" },
                              { label: "Allmail known-sender", value: allmail_labeled_known_count ?? "—" },
                              { label: "Allmail unknown-sender", value: allmail_labeled_unknown_count ?? "—" },
                              { label: "Inbox known-sender", value: inbox_labeled_known_shallow_count != null ? `${inbox_labeled_known_shallow_count}${inbox_labeled_known_has_more ? "+" : ""}` : "—" },
                              { label: unknownCountLabel, value: inbox_labeled_unknown_shallow_count != null ? `${inbox_labeled_unknown_shallow_count}${inbox_labeled_unknown_has_more ? "+" : ""}` : "—" },
                              { label: "Inbox unlabeled", value: inbox_unlabeled_deep_count ?? "—" },
                              { label: "Allmail unlabeled", value: state.data.allmail_unlabeled_first_page_count != null ? `${state.data.allmail_unlabeled_first_page_count}${state.data.allmail_unlabeled_first_page_count >= 500 ? "+" : ""}` : "—" },
                            ]}
                          />
                          <InfoSection
                            iconElement={<span className={cn("inline-block h-2 w-2 rounded-full", severity === "error" || state.data.gmail_error ? "bg-destructive" : severity === "warning" ? "bg-yellow-500" : "bg-green-500")} />}
                            title="System health"
                            errorCode={state.data.gmail_error?.code}
                            action={state.data.gmail_error?.label === "error.gmail.auth_expired" ? (
                              <a
                                href={`${API_URL}/oauth/start?force_consent=true&return_to=${encodeURIComponent(window.location.origin)}`}
                                className="text-[10px] px-2 py-0.5 rounded-full border border-destructive text-destructive hover:bg-destructive/10 transition-colors"
                              >
                                Re-authenticate
                              </a>
                            ) : undefined}
                            rows={[
                              { label: "Last fetched", value: state.data.last_fetched_at ? formatRelativeTime(new Date(state.data.last_fetched_at)) : "—" },
                              { label: "Last labeled", value: state.data.last_labeled_at ? formatRelativeTime(new Date(state.data.last_labeled_at)) : "—" },
                              { label: "Newest email", value: state.data.newest_mail_at ? formatRelativeTime(new Date(state.data.newest_mail_at), "old") : "—" },
                              { label: "Newest labeled", value: state.data.newest_labeled_at ? formatRelativeTime(new Date(state.data.newest_labeled_at), "old") : "—" },
                            ]}
                          />
                        </>
                      );
                    })()}
                    <div className="flex justify-between gap-4 items-center mt-3 pt-2 border-t border-border/50">
                      <span className="text-xs font-semibold">Noise reduced</span>
                      <span className="text-xs font-semibold tabular-nums">
                        {allmail_labeled_total_count !== null && allmail_labeled_unknown_count !== null && allmail_labeled_total_count > 0
                          ? `${Math.round((allmail_labeled_unknown_count / allmail_labeled_total_count) * 100)}%`
                          : "—"}
                      </span>
                    </div>
                    {isKnownSender && (
                      <InfoSection
                        icon={topSendersLoading ? Loader2 : CheckCircle}
                        iconColor={topSendersLoading ? "" : "text-green-500"}
                        iconSpin={topSendersLoading}
                        title="Top known senders — unread inbox"
                        rows={topSenders.length > 0
                          ? topSenders.map((sender) => ({
                              label: <a href={`https://mail.google.com/mail/u/?authuser=${encodeURIComponent(email)}#search/from:${encodeURIComponent(sender.email)}+is:unread+in:inbox`} target="_blank" rel="noopener noreferrer" className="hover:underline" title="Open in Gmail">{sender.email}</a>,
                              value: sender.count,
                            }))
                          : [{ label: "No unread known-sender messages", value: "—" }]
                        }
                      />
                    )}
                    {isKnownSender && (
                      <InfoSection
                        icon={topSendersLoading ? Loader2 : CheckCircle}
                        iconColor={topSendersLoading ? "" : "text-green-500"}
                        iconSpin={topSendersLoading}
                        title="Top known domains — unread inbox"
                        rows={topDomains.length > 0
                          ? topDomains.map((d) => ({
                              label: <a href={`https://mail.google.com/mail/u/?authuser=${encodeURIComponent(email)}#search/from:${encodeURIComponent(d.domain)}+is:unread+in:inbox`} target="_blank" rel="noopener noreferrer" className="hover:underline" title="Open in Gmail">{d.domain}</a>,
                              value: d.count,
                            }))
                          : [{ label: "No unread known-sender messages", value: "—" }]
                        }
                      />
                    )}
                  </div>
                );
              })}
            </div>
            <div className="flex items-center justify-between gap-4">
              {lastUpdated !== null ? (
                <span className="text-xs text-muted-foreground min-w-0 truncate">
                  Last updated{" "}
                  <span data-testid="last-updated-time">
                    {formatRelativeTime(lastUpdated)}
                  </span>
                </span>
              ) : (
                <span />
              )}
              <button
                onClick={handleRefresh}
                disabled={refreshing}
                aria-label="Refresh stats"
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50 shrink-0"
              >
                <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} />
                <span>Refresh now</span>
              </button>
            </div>
          </div>

          {state.data.recent_events?.length > 0 && (
            <details className="w-full text-xs">
              <summary className="text-muted-foreground cursor-pointer hover:text-foreground">
                Activity log ({state.data.recent_events.length})
              </summary>
              <div className="mt-2 max-h-48 overflow-y-auto rounded border border-border bg-muted/30 px-3 py-2 space-y-1">
                {state.data.recent_events.map((evt, i) => (
                  <div key={i} className="flex gap-2">
                    <span className="text-muted-foreground/50 tabular-nums shrink-0">
                      {new Date(evt.timestamp).toLocaleTimeString()}
                    </span>
                    <span className={evt.event_type === "error" ? "text-destructive" : "text-muted-foreground"}>
                      {evt.message}
                    </span>
                  </div>
                ))}
              </div>
            </details>
          )}

        </div>
      </main>
    </>
  );
}
