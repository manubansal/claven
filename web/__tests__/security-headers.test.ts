import { describe, expect, it } from "vitest";

import nextConfig from "@/next.config";

async function headersFor(path: string): Promise<Record<string, string>> {
  const rules = (await nextConfig.headers?.()) ?? [];
  const rule = rules.find((r) => r.source === "/:path*");
  expect(rule, `no header rule covering ${path}`).toBeDefined();
  return Object.fromEntries(rule!.headers.map((h) => [h.key.toLowerCase(), h.value]));
}

describe("security headers (CASA M1)", () => {
  it("applies to every path", async () => {
    const headers = await headersFor("/dashboard");
    expect(headers["strict-transport-security"]).toBe("max-age=31536000; includeSubDomains");
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["x-frame-options"]).toBe("DENY");
    expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
    expect(headers["permissions-policy"]).toContain("camera=()");
  });

  it("has a CSP that allows only the site itself and the Claven API", async () => {
    const csp = (await headersFor("/"))["content-security-policy"];
    const directives = Object.fromEntries(
      csp.split(";").map((d) => d.trim().split(/\s+/)).map(([name, ...values]) => [name, values]),
    );
    expect(directives["default-src"]).toEqual(["'self'"]);
    expect(directives["connect-src"]).toEqual(["'self'", "https://api.claven.app"]);
    expect(directives["frame-ancestors"]).toEqual(["'none'"]);
    expect(directives["object-src"]).toEqual(["'none'"]);
    expect(directives["script-src"]).not.toContain("'unsafe-eval'");
  });
});

describe("static asset headers (public/_headers)", () => {
  it("match next.config.ts for every header that applies to assets", async () => {
    const { readFileSync } = await import("fs");
    const lines = readFileSync(`${__dirname}/../public/_headers`, "utf-8").split("\n");
    expect(lines).toContain("/*");
    const assetHeaders = Object.fromEntries(
      lines
        .filter((line) => /^\s+\S+:/.test(line))
        .map((line) => {
          const [name, ...rest] = line.trim().split(":");
          return [name.toLowerCase(), rest.join(":").trim()];
        }),
    );
    const pageHeaders = await headersFor("/");
    for (const name of ["strict-transport-security", "x-content-type-options", "x-frame-options", "referrer-policy"]) {
      expect(assetHeaders[name], name).toBe(pageHeaders[name]);
    }
  });
});
