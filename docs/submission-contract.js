(function (root) {
  "use strict";

  function stableStringify(value) {
    if (value === null || typeof value !== "object") {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
      return "[" + value.map(stableStringify).join(",") + "]";
    }
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map(function (key) {
          return JSON.stringify(key) + ":" + stableStringify(value[key]);
        })
        .join(",") +
      "}"
    );
  }

  function normalizedText(value) {
    return String(value === undefined || value === null ? "" : value)
      .trim()
      .normalize("NFC");
  }

  function canonicalIso(value) {
    return new Date(normalizedText(value)).toISOString();
  }

  function requiredInteger(value, field) {
    var result = Number(value);
    if (!Number.isInteger(result)) {
      throw new Error(field + " must be an integer");
    }
    return result;
  }

  function normalizeDigestBasis(raw) {
    var responses = raw.responses
      .map(function (response) {
        var itemQualityIncluded =
          response.item_quality_code !== undefined ||
          response.item_quality_note !== undefined;
        var normalized = {
          display_position: requiredInteger(
            response.display_position,
            "response.display_position"
          ),
          pilot_item_id: normalizedText(response.pilot_item_id),
          choice: requiredInteger(response.choice, "response.choice"),
          reason_code: String(response.reason_code),
          confidence: requiredInteger(
            response.confidence,
            "response.confidence"
          ),
          reason_note: normalizedText(response.reason_note || ""),
          started_at: canonicalIso(response.started_at),
          finished_at: canonicalIso(response.finished_at),
          active_duration_seconds: requiredInteger(
            response.active_duration_seconds,
            "response.active_duration_seconds"
          )
        };
        if (itemQualityIncluded) {
          normalized.item_quality_code = normalizedText(
            response.item_quality_code === undefined
              ? "NONE"
              : response.item_quality_code
          );
          normalized.item_quality_note = normalizedText(
            response.item_quality_note || ""
          );
        }
        return normalized;
      })
      .sort(function (left, right) {
        return left.display_position - right.display_position;
      });

    return {
      consent: {
        accepted: raw.consent.accepted === true,
        version: normalizedText(raw.consent.version),
        accepted_at: canonicalIso(raw.consent.accepted_at)
      },
      feedback: {
        fatigue_1to5: requiredInteger(
          raw.feedback.fatigue_1to5,
          "feedback.fatigue_1to5"
        ),
        zero_vs_99_explanation: normalizedText(
          raw.feedback.zero_vs_99_explanation
        ),
        change_vs_stance_explanation: normalizedText(
          raw.feedback.change_vs_stance_explanation
        ),
        ui_error_note: normalizedText(raw.feedback.ui_error_note || "")
      },
      instrument_sha256: String(raw.instrument_sha256),
      responses: responses,
      session: {
        started_at: canonicalIso(raw.session.started_at),
        finished_at: canonicalIso(raw.session.finished_at),
        active_duration_seconds: requiredInteger(
          raw.session.active_duration_seconds,
          "session.active_duration_seconds"
        )
      }
    };
  }

  async function sha256Hex(text) {
    var bytes = new TextEncoder().encode(text);
    var digest = await root.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map(function (value) {
        return value.toString(16).padStart(2, "0");
      })
      .join("");
  }

  async function finalizeDigest(raw) {
    var basis = normalizeDigestBasis(raw);
    return {
      basis: basis,
      payload_sha256: await sha256Hex(stableStringify(basis))
    };
  }

  root.PILOT_SUBMISSION_CONTRACT = Object.freeze({
    stableStringify: stableStringify,
    normalizeDigestBasis: normalizeDigestBasis,
    sha256Hex: sha256Hex,
    finalizeDigest: finalizeDigest
  });
})(typeof window === "undefined" ? globalThis : window);
