export const steps = [
  { label: 'CCCD trước' }, { label: 'CCCD sau' }, { label: 'GPLX trước' },
  { label: 'GPLX sau', optional: true }, { label: 'Selfie', selfie: true },
];

export function quality(data, w, h, nativeWidth, nativeHeight) {
  const gray = new Float32Array(w * h);
  let brightness = 0, bright = 0, edges = 0;
  for (let i = 0; i < gray.length; i++) {
    const j = 4 * i;
    const value = .299 * data[j] + .587 * data[j + 1] + .114 * data[j + 2];
    gray[i] = value;
    brightness += value;
    if (Math.min(data[j], data[j + 1], data[j + 2]) >= 248) bright++;
  }
  for (let y = 1; y < h; y++) {
    for (let x = 1; x < w; x++) {
      const i = y * w + x;
      edges += Math.abs(gray[i] - gray[i - 1]) + Math.abs(gray[i] - gray[i - w]);
    }
  }
  return [
    ...(Math.min(nativeWidth, nativeHeight) < 480 ? ['SMALL'] : []),
    ...(brightness / gray.length < 55 ? ['DARK'] : []),
    ...(bright / gray.length > .15 ? ['BRIGHT'] : []),
    ...(edges / gray.length < 6 ? ['LOW_DETAIL'] : []),
  ];
}
