// get_weather — live current weather + short forecast via Open-Meteo (no API key).
//
// Egress: requires the `ava-weather` policy (api.open-meteo.com,
// geocoding-api.open-meteo.com on :443). See ../policies/ava-weather.yaml.

// Default location when the caller doesn't pass one. Blank unless the owner set
// AVA_DEFAULT_LOCATION ("City, Region") — the persona already tells the model the
// user's home place, so it normally passes an explicit location.
const DEFAULT_LOCATION_NAME = (process.env.AVA_DEFAULT_LOCATION || '').trim();

const WMO = {
  0: 'clear sky', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast',
  45: 'fog', 48: 'depositing rime fog',
  51: 'light drizzle', 53: 'moderate drizzle', 55: 'dense drizzle',
  56: 'light freezing drizzle', 57: 'dense freezing drizzle',
  61: 'slight rain', 63: 'moderate rain', 65: 'heavy rain',
  66: 'light freezing rain', 67: 'heavy freezing rain',
  71: 'slight snow', 73: 'moderate snow', 75: 'heavy snow', 77: 'snow grains',
  80: 'slight rain showers', 81: 'moderate rain showers', 82: 'violent rain showers',
  85: 'slight snow showers', 86: 'heavy snow showers',
  95: 'thunderstorm', 96: 'thunderstorm with slight hail', 99: 'thunderstorm with heavy hail',
};

async function geocode(name, http) {
  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(name)}&count=1&language=en&format=json`;
  const j = await http.getJson(url);
  const hit = j && j.results && j.results[0];
  if (!hit) throw new Error(`couldn't find a place called "${name}"`);
  const label = [hit.name, hit.admin1, hit.country_code].filter(Boolean).join(', ');
  return { name: label, latitude: hit.latitude, longitude: hit.longitude };
}

export default {
  name: 'get_weather',
  description: "Get the REAL, live current weather and a short multi-day forecast for a location. "
    + "This tool has working internet access and returns up-to-date data, so ALWAYS call it for any "
    + "weather, temperature, or forecast question instead of declining or guessing. "
    + "If no location is given, uses the user's home location when one is configured; otherwise ask which place they mean.",
  inputSchema: {
    type: 'object',
    properties: {
      location: { type: 'string', description: 'City and region/country, e.g. "Austin, TX" or "Tokyo". Optional.' },
      days: { type: 'integer', description: 'Number of forecast days (1-7). Default 3.', minimum: 1, maximum: 7 },
    },
  },
  handler: async (args, { http }) => {
    const loc = args && args.location && String(args.location).trim();
    const name = loc || DEFAULT_LOCATION_NAME;
    if (!name) {
      return 'Which location? Tell me a city and region, e.g. "Austin, TX".';
    }
    const place = await geocode(name, http);
    const days = Math.min(Math.max(parseInt(args && args.days, 10) || 3, 1), 7);
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${place.latitude}&longitude=${place.longitude}`
      + `&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m`
      + `&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code`
      + `&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&timezone=auto&forecast_days=${days}`;
    const j = await http.getJson(url);
    const c = j.current || {};
    const desc = WMO[c.weather_code] || 'unknown conditions';
    const lines = [];
    lines.push(`Weather for ${place.name}:`);
    lines.push(`Now: ${Math.round(c.temperature_2m)}°F (feels like ${Math.round(c.apparent_temperature)}°F), ${desc}, `
      + `humidity ${c.relative_humidity_2m}%, wind ${Math.round(c.wind_speed_10m)} mph.`);
    const d = j.daily || {};
    if (d.time) {
      for (let i = 0; i < d.time.length; i++) {
        const day = i === 0 ? 'Today' : new Date(d.time[i] + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'long' });
        lines.push(`${day}: high ${Math.round(d.temperature_2m_max[i])}°F / low ${Math.round(d.temperature_2m_min[i])}°F, `
          + `${WMO[d.weather_code[i]] || 'mixed'}, ${d.precipitation_probability_max[i]}% chance of precip.`);
      }
    }
    return lines.join('\n');
  },
};
