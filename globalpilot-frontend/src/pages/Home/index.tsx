import { Link } from 'react-router'
import { useRequireAuthNav } from '../../auth/useRequireAuthNav'

const FEATURES = [
  {
    index: '01',
    name: 'Research',
    to: '/research',
    desc: 'Map markets, audience pain points, competitors, and content opportunities before you publish.',
    requiresAuth: true,
  },
  {
    index: '02',
    name: 'Marketing',
    to: '/marketing',
    desc: 'Orchestrate LinkedIn, YouTube, and SEO workflows on a visual canvas powered by brand context.',
    requiresAuth: true,
  },
  {
    index: '03',
    name: 'Assets',
    to: '/assets',
    desc: 'Keep brand voice, site facts, channel notes, and uploaded txt / PDF documents in one living library.',
    requiresAuth: false,
  },
] as const

const Home = () => {
  const goProtected = useRequireAuthNav()

  return (
    <main className="gp-home">
      {/* Brand Center — first viewport */}
      <section className="gp-section gp-brand-center" aria-labelledby="brand-center-title">
        <div className="gp-brand-center__stage" aria-hidden>
          <div className="gp-brand-center__grid" />
          <div className="gp-brand-center__orb gp-animate-drift" />
          <div className="gp-brand-center__horizon" />
        </div>

        <div className="gp-section__inner relative">
          <p
            id="brand-center-title"
            className="gp-brand-center__brand gp-animate-fade-up"
          >
            GlobalPilot
          </p>
          <h1 className="gp-brand-center__headline gp-animate-fade-up gp-delay-1">
            Understand the brand. Then scale every channel.
          </h1>
          <p className="gp-brand-center__lede gp-animate-fade-up gp-delay-2">
            An AI marketing ops desk for overseas growth — capture brand, site,
            competitors, and channels once, then reuse them across LinkedIn,
            YouTube, and SEO.
          </p>
          <div className="gp-brand-center__actions gp-animate-fade-up gp-delay-3">
            <button
              type="button"
              className="gp-btn gp-btn--primary"
              onClick={() => goProtected('/research')}
            >
              Explore research
            </button>
          </div>
          <div
            className="gp-brand-center__rule gp-animate-line gp-delay-4"
            aria-hidden
          />
        </div>
      </section>

      {/* Feature entrances */}
      <section className="gp-section gp-features" aria-labelledby="features-title">
        <div className="gp-section__inner">
          <div className="gp-features__intro">
            <p className="gp-features__label">Workspace</p>
            <h2 id="features-title" className="gp-features__title">
              Move from insight to content without losing context.
            </h2>
          </div>

          <ul className="gp-features__list">
            {FEATURES.map((feature) => (
              <li key={feature.to}>
                {feature.requiresAuth ? (
                  <button
                    type="button"
                    className="gp-feature-row w-full text-left"
                    onClick={() => goProtected(feature.to)}
                  >
                    <div>
                      <span className="gp-feature-row__index">{feature.index}</span>
                      <p className="gp-feature-row__name">{feature.name}</p>
                      <p className="gp-feature-row__desc">{feature.desc}</p>
                    </div>
                    <span className="gp-feature-row__go">
                      Enter
                      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                        <path
                          d="M3 8h10M9 4l4 4-4 4"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>
                  </button>
                ) : (
                  <Link to={feature.to} className="gp-feature-row">
                    <div>
                      <span className="gp-feature-row__index">{feature.index}</span>
                      <p className="gp-feature-row__name">{feature.name}</p>
                      <p className="gp-feature-row__desc">{feature.desc}</p>
                    </div>
                    <span className="gp-feature-row__go">
                      Enter
                      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                        <path
                          d="M3 8h10M9 4l4 4-4 4"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* Bottom hero */}
      <section className="gp-section gp-bottom-hero" aria-labelledby="bottom-hero-title">
        <div className="gp-bottom-hero__map" aria-hidden />
        <div className="gp-bottom-hero__glow gp-animate-drift" aria-hidden />

        <div className="gp-section__inner relative">
          <p id="bottom-hero-title" className="gp-bottom-hero__brand">
            Pilot your
            <br />
            next market.
          </p>
          <p className="gp-bottom-hero__copy">
            Start with research, lock the brand system, then launch channel-ready
            campaigns that stay on-message everywhere.
          </p>
          <div className="gp-bottom-hero__actions">
            <button
              type="button"
              className="gp-btn gp-btn--on-ink"
              onClick={() => goProtected('/marketing')}
            >
              Launch marketing canvas
            </button>
            <button
              type="button"
              className="gp-btn gp-btn--ghost-on-ink"
              onClick={() => goProtected('/research')}
            >
              Explore research
            </button>
          </div>
        </div>
      </section>
    </main>
  )
}

export default Home
