export const ASSET_KINDS = [
  { id: "voice", label: "Brand voice" },
  { id: "site", label: "Site facts" },
  { id: "channels", label: "Channel notes" },
  { id: "briefs", label: "Reusable briefs" },
  { id: "docs", label: "Brand documents" },
] as const;

export type AssetKind = (typeof ASSET_KINDS)[number]["id"];

export type BrandAsset = {
  id: string;
  kind: AssetKind;
  title: string;
  summary: string;
  body: string;
  updatedAt: string;
};

export const MOCK_ASSETS: BrandAsset[] = [
  {
    id: "voice-core",
    kind: "voice",
    title: "Core voice",
    summary: "Calm, specific, operator-to-operator. No hype, no purple AI.",
    body: "Write like a senior operator briefing a peer.\n\nDo: concrete artifacts, named roles, one claim per paragraph.\nDon’t: “unlock”, “supercharge”, or founder-myth storytelling.\n\nWe sound like ocean navigation — cool mist, ink type, a single sea-teal signal.",
    updatedAt: "11 Aug 2026",
  },
  {
    id: "voice-linkedin",
    kind: "voice",
    title: "LinkedIn register",
    summary: "First-person, one decision, one miss. No carousel filler.",
    body: "Open with the week’s decision.\nName the audience in sentence one.\nClose with a reusable brief, not a CTA stack.",
    updatedAt: "9 Aug 2026",
  },
  {
    id: "site-product",
    kind: "site",
    title: "What Harbor is",
    summary: "Async standup notes that become a weekly exec pulse.",
    body: "Harbor turns scattered status into a Friday brief.\nICP: engineering managers at 50–200 person US SaaS.\nDifferentiator: the brief is the source of truth for LinkedIn, YouTube, and SEO — not a chat log.",
    updatedAt: "10 Aug 2026",
  },
  {
    id: "site-objections",
    kind: "site",
    title: "Objections",
    summary: "“We already have Slack standups.” “Another AI seat.”",
    body: "Slack standups stay in chat. Harbor is the rewrite managers already do by hand.\nWe do not sell a copilot. We sell three hours back and a brief you can publish.",
    updatedAt: "8 Aug 2026",
  },
  {
    id: "ch-linkedin",
    kind: "channels",
    title: "LinkedIn",
    summary: "Founder notes. Two posts a week from the research report.",
    body: "Cadence: Tue insight, Thu proof.\nFormat: 120–180 words, no emoji, one link to a brief.\nProof: named role + one metric from the Harbor week.",
    updatedAt: "12 Aug 2026",
  },
  {
    id: "ch-youtube",
    kind: "channels",
    title: "YouTube",
    summary: "Loom-length walkthroughs. Screen, voice, no keynote.",
    body: "Length: 6–9 minutes.\nTitle pattern: “How we close the week as an EM”.\nEnd card: download the brief template. No subscribe begging.",
    updatedAt: "7 Aug 2026",
  },
  {
    id: "ch-seo",
    kind: "channels",
    title: "SEO",
    summary: "Comparison and “standup alternative” clusters.",
    body: "Pillars: standup alternatives, Range vs Harbor, Geekbot vs Harbor, weekly pulse template.\nEvery page cites the brand library. No synonym stuffing.",
    updatedAt: "6 Aug 2026",
  },
  {
    id: "brief-q3",
    kind: "briefs",
    title: "Q3 US EM campaign",
    summary: "From Harbor research — Friday brief, Monday LinkedIn.",
    body: "Audience: US engineering managers, 50–200.\nOffer: weekly pulse template.\nChannels: LinkedIn notes + SEO comparison cluster + one YouTube walkthrough.\nDo not invent metrics. Pull from the Harbor research report.",
    updatedAt: "12 Aug 2026",
  },
];
