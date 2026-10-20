import { render, screen } from "@testing-library/react";
import { useSearchParams } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import Home from "@/app/page";
import PrivacyPage from "@/app/privacy/page";
import TermsPage from "@/app/terms/page";
import { CONTACT_EMAIL } from "@/lib/constants";

vi.mock("next/navigation");

const LIMITED_USE_STATEMENT =
  "Claven's use and transfer of information received from Google APIs to any other app will adhere to Google API Services User Data Policy, including the Limited Use requirements.";

describe("Privacy policy page", () => {
  it("has a Privacy Policy heading", () => {
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: /privacy policy/i })).toBeInTheDocument();
  });

  it("contains Google's Limited Use disclosure verbatim", () => {
    const { container } = render(<PrivacyPage />);
    expect(container.textContent).toContain(LIMITED_USE_STATEMENT);
  });

  it("links to the Google API Services User Data Policy", () => {
    render(<PrivacyPage />);
    expect(
      screen.getByRole("link", { name: /Google API Services User Data Policy/ }),
    ).toHaveAttribute("href", "https://developers.google.com/terms/api-services-user-data-policy");
  });

  it("names the gmail.modify scope it requests", () => {
    const { container } = render(<PrivacyPage />);
    expect(container.textContent).toContain("gmail.modify");
  });

  it("explains how to request deletion via the contact email", () => {
    const { container } = render(<PrivacyPage />);
    expect(container.textContent).toMatch(/delet/i);
    expect(container.textContent).toContain(CONTACT_EMAIL);
  });
});

describe("Terms of service page", () => {
  it("has a Terms of Service heading", () => {
    render(<TermsPage />);
    expect(screen.getByRole("heading", { level: 1, name: /terms of service/i })).toBeInTheDocument();
  });

  it("shows the contact email", () => {
    const { container } = render(<TermsPage />);
    expect(container.textContent).toContain(CONTACT_EMAIL);
  });

  it("links to the privacy policy", () => {
    render(<TermsPage />);
    expect(screen.getByRole("link", { name: /privacy policy/i })).toHaveAttribute("href", "/privacy");
  });
});

describe("Home page legal links", () => {
  it("links to the privacy policy and terms of service", () => {
    vi.mocked(useSearchParams).mockReturnValue({
      get: () => null,
    } as unknown as ReturnType<typeof useSearchParams>);
    render(<Home />);
    expect(screen.getByRole("link", { name: "Privacy" })).toHaveAttribute("href", "/privacy");
    expect(screen.getByRole("link", { name: "Terms" })).toHaveAttribute("href", "/terms");
  });
});
