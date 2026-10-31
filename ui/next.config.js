/** @type {import('next').NextConfig} */
const nextConfig = {
  // Long agent turns stream over SSE through the rewrite below; the default
  // proxy timeout (30 s) would cut them off mid-response.
  experimental: {
    proxyTimeout: 3_600_000,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        // Backend address; override with ALITA_API_URL (e.g. in Docker).
        destination: `${(process.env.ALITA_API_URL || 'http://localhost:8080').replace(/\/$/, '')}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
