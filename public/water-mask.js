'use strict';
(function(root,factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BayWaterMask = factory();
})(typeof globalThis === 'object' ? globalThis : this,function() {
  const radians = Math.PI / 180;
  const mercator = lat => Math.log(Math.tan(Math.PI / 4 + lat * radians / 2)) / radians;
  const validBounds = bounds => Array.isArray(bounds) && bounds.length === 4 && bounds.every(Number.isFinite)
    && bounds[0] < bounds[2] && bounds[1] < bounds[3];
  function inRing(x,y,ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 2; i < ring.length; j = i, i += 2) {
      const ax = ring[j], ay = ring[j + 1], bx = ring[i], by = ring[i + 1];
      const dx = bx - ax, dy = by - ay, length = dx * dx + dy * dy;
      const dot = (x - ax) * dx + (y - ay) * dy;
      if (length && Math.abs((x - ax) * dy - (y - ay) * dx) < 1e-12 && dot >= 0 && dot <= length) return true;
      if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
    }
    return inside;
  }
  function create(payload) {
    if (payload?.version !== 1 || !validBounds(payload.bounds) || !Array.isArray(payload.polygons) || !payload.polygons.length) {
      throw new Error('Invalid water boundary data');
    }
    const polygons = payload.polygons.map(polygon => {
      if (!validBounds(polygon.bounds) || !Array.isArray(polygon.rings) || !polygon.rings.length) throw new Error('Invalid water polygon');
      const rings = polygon.rings.map(ring => {
        if (!Array.isArray(ring) || ring.length < 4) throw new Error('Invalid water polygon ring');
        const projected = new Float64Array(ring.length * 2);
        ring.forEach((point,i) => {
          if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite)
            || Math.abs(point[0]) > 180 || Math.abs(point[1]) >= 85.051129) throw new Error('Invalid water coordinate');
          projected[i * 2] = point[0]; projected[i * 2 + 1] = mercator(point[1]);
        });
        return projected;
      });
      return {bounds:polygon.bounds.slice(),rings};
    });
    return {
      isWater(lat,lng) {
        if (!Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) >= 85.051129) return false;
        const y = mercator(lat);
        return polygons.some(({bounds,rings}) => {
          if (lng < bounds[0] || lng > bounds[2] || lat < bounds[1] || lat > bounds[3]) return false;
          let inside = false;
          for (const ring of rings) if (inRing(lng,y,ring)) inside = !inside;
          return inside;
        });
      },
      erase(context,origin) {
        const [[north,west],[south,east]] = origin.bounds;
        if (!(north > south && east > west && origin.width > 0 && origin.height > 0)) throw new Error('Invalid raster bounds');
        const top = mercator(north), bottom = mercator(south);
        const scaleX = origin.width / (east - west), scaleY = origin.height / (top - bottom);
        context.save();
        try {
          context.globalCompositeOperation = 'destination-out';
          context.globalAlpha = 1; context.fillStyle = '#000';
          for (const {bounds,rings} of polygons) {
            if (bounds[2] < west || bounds[0] > east || bounds[3] < south || bounds[1] > north) continue;
            context.beginPath();
            for (const ring of rings) {
              context.moveTo((ring[0] - west) * scaleX,(top - ring[1]) * scaleY);
              for (let i = 2; i < ring.length; i += 2) context.lineTo((ring[i] - west) * scaleX,(top - ring[i + 1]) * scaleY);
              context.closePath();
            }
            // Erase components separately: overlaps remain water, and ring
            // winding cannot accidentally paint over an island inside a lake.
            context.fill('evenodd');
          }
        } finally { context.restore(); }
      }
    };
  }
  return {create};
});
