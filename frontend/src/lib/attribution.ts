// One-line credit per live source, shown on the Live page for the sources that are enabled.
// Keep in sync with NOTICE and the credits list on the System page.
export const SOURCE_CREDITS: Record<string, { label: string; url: string | null; note: string }> = {
  'adsb.fi': { label: 'adsb.fi', url: 'https://adsb.fi', note: 'open data' },
  'adsb.lol': { label: 'adsb.lol', url: 'https://adsb.lol', note: 'ODbL' },
  flyitalyadsb: { label: 'FlyItalyADSB', url: 'https://flyitalyadsb.com', note: 'CC BY-SA 4.0' },
  opensky: { label: 'The OpenSky Network', url: 'https://opensky-network.org', note: 'non-commercial' },
  receiver: { label: 'your receiver', url: null, note: '1090 MHz' },
  'receiver-uat': { label: 'your receiver', url: null, note: '978 MHz UAT' },
}

export const DEFAULT_SOURCES = ['adsb.fi', 'adsb.lol']
