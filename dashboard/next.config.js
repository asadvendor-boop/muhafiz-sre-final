const path = require("path");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname),
  // Hide the floating "N" dev indicator during recording
  devIndicators: false,
  // Allow loading images from external sources (avatars, diagrams)
  images: {
    unoptimized: true,
  },
  // Proxy API requests to the FastAPI gateway during development
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.GATEWAY_INTERNAL_URL || "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
