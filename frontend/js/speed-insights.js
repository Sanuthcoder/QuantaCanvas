/**
 * Vercel Speed Insights initialization for QuantaCanvas
 * This script loads and initializes Vercel Speed Insights for performance monitoring
 */

import { injectSpeedInsights } from 'https://cdn.jsdelivr.net/npm/@vercel/speed-insights@1/dist/index.mjs';

// Initialize Speed Insights
injectSpeedInsights({
  debug: false, // Set to true for development debugging
  sampleRate: 1.0, // Track 100% of page loads (adjust if needed for cost management)
});
