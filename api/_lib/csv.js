function flattenRow(obj, prefix = "", output = {}) {
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    for (const [key, value] of Object.entries(obj)) {
      const nextKey = prefix ? `${prefix}.${key}` : key;
      if (value && typeof value === "object" && !Array.isArray(value)) {
        flattenRow(value, nextKey, output);
      } else if (Array.isArray(value)) {
        output[nextKey] = JSON.stringify(value);
      } else {
        output[nextKey] = value;
      }
    }
  } else {
    output[prefix || "value"] = obj;
  }
  return output;
}

function csvEscape(value) {
  if (value === null || value === undefined) return "";
  const str = String(value);
  if (str.includes('"') || str.includes(",") || str.includes("\n") || str.includes("\r")) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function toCsv(rows) {
  const flattened = rows.map((row) => flattenRow(row));
  const headers = Array.from(
    flattened.reduce((set, row) => {
      for (const key of Object.keys(row)) set.add(key);
      return set;
    }, new Set())
  ).sort();

  const lines = [];
  lines.push(headers.join(","));
  for (const row of flattened) {
    const line = headers.map((key) => csvEscape(row[key])).join(",");
    lines.push(line);
  }
  return "\ufeff" + lines.join("\n");
}

module.exports = {
  toCsv,
};
