/**
 * Edge Function: xray-patient-search
 *
 * Google-style single-field patient search for external X-ray .NET app.
 * Auto-detects search type: UUID, MRN, DOB, phone, or name.
 * Returns patient list with id, name, dob, phone, and profile picture URL.
 *
 * Auth: x-api-key header must match a token in facility_external_tokens table.
 * Token auto-determines the facility (no secrets needed per facility).
 *
 * POST body: { "q": "search term" }
 *   - Empty/blank "q" returns the most recently added patients for the facility.
 *
 * Returns: { "patients": [ { id, first_name, last_name, dob, phone, email, profile_picture_url } ] }
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-api-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// ── Helpers ──

/** Detect if term is a UUID */
function isUuid(term: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(term);
}

/** Detect if term looks like a phone number (3+ digits, no letters) */
function isPhoneSearch(term: string): boolean {
  const digits = term.replace(/\D/g, "");
  const hasAlpha = /[a-zA-Z]/.test(term);
  return digits.length >= 3 && !hasAlpha;
}

/** Detect if term is a date pattern (MM/DD/YYYY, YYYY-MM-DD, MMDDYYYY) */
function detectDatePattern(term: string): { isDate: boolean; isoDate: string | null } {
  const cleaned = term.trim();

  // MM/DD/YYYY or MM-DD-YYYY
  const mdyMatch = cleaned.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})$/);
  if (mdyMatch) {
    const [_, mm, dd, yyyy] = mdyMatch;
    const isoDate = `${yyyy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}`;
    const parsed = new Date(isoDate);
    if (!isNaN(parsed.getTime())) return { isDate: true, isoDate };
  }

  // YYYY-MM-DD
  const isoMatch = cleaned.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (isoMatch) {
    const parsed = new Date(cleaned);
    if (!isNaN(parsed.getTime())) return { isDate: true, isoDate: cleaned };
  }

  // MMDDYYYY (8 digits no separators)
  const digits = cleaned.replace(/\D/g, "");
  if (digits.length === 8 && /^\d{8}$/.test(cleaned)) {
    const mm = digits.slice(0, 2);
    const dd = digits.slice(2, 4);
    const yyyy = digits.slice(4, 8);
    const isoDate = `${yyyy}-${mm}-${dd}`;
    const parsed = new Date(isoDate);
    if (!isNaN(parsed.getTime())) return { isDate: true, isoDate };
  }

  return { isDate: false, isoDate: null };
}

/** Convert GCS URL to proxy URL for profile pictures */
function getProxyUrl(gcsUrl: string, supabaseUrl: string): string {
  if (!gcsUrl) return "";
  if (gcsUrl.includes("storage.googleapis.com/purechart-patient-files")) {
    return `${supabaseUrl}/functions/v1/get-signed-url?url=${encodeURIComponent(gcsUrl)}`;
  }
  return gcsUrl;
}

/** Verify patient belongs to facility */
async function verifyFacility(supabase: any, patientId: string, facilityId: string): Promise<boolean> {
  const { data } = await supabase
    .from("patients_to_facilities")
    .select("patient_id")
    .eq("patient_id", patientId)
    .eq("facility_id", facilityId)
    .is("deleted_at", null)
    .maybeSingle();
  return !!data;
}

// ── Main ──

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    if (req.method !== "POST") {
      return new Response(
        JSON.stringify({ error: "Method not allowed" }),
        { status: 405, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // ── Auth: look up token → facility from DB ──
    const token = req.headers.get("x-api-key");
    if (!token) {
      return new Response(
        JSON.stringify({ error: "Unauthorized — x-api-key header required" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
    const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY);

    // Look up token in facility_external_tokens
    const { data: tokenRecord, error: tokenError } = await supabase
      .from("facility_external_tokens")
      .select("id, facility_id")
      .eq("token", token)
      .eq("service_name", "xray")
      .eq("is_enabled", true)
      .is("deleted_at", null)
      .maybeSingle();

    if (tokenError) {
      console.error("[xray-patient-search] Token lookup error:", tokenError);
      return new Response(
        JSON.stringify({ error: "Server error" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    if (!tokenRecord) {
      return new Response(
        JSON.stringify({ error: "Unauthorized — invalid or disabled token" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const facilityId = tokenRecord.facility_id;

    // Update last_used_at (fire and forget)
    supabase
      .from("facility_external_tokens")
      .update({ last_used_at: new Date().toISOString() })
      .eq("id", tokenRecord.id)
      .then(() => {});

    // ── Parse request ──
    const { q } = await req.json();
    const term = (q && typeof q === "string") ? q.trim() : "";

    console.log(`[xray-patient-search] q="${term}" facility=${facilityId}`);

    let patients: any[] = [];
    const LIMIT = 15;
    const PATIENT_COLS = "id, first_name, last_name, middle_name, preferred_name, medical_record_number, status, created_at, is_locked";

    // ── 0. Empty query → return today's scheduled patients for this facility ──
    if (!term) {
      console.log("[xray-patient-search] Route: Today's patients (empty query)");

      // Today's date in facility local time (apmt_start_time is stored without TZ)
      // Use America/Chicago for CST/CDT — handles daylight saving automatically
      const today = new Date().toLocaleDateString("en-CA", { timeZone: "America/Chicago" }); // "2026-04-01"
      const todayStart = `${today} 00:00`;
      const todayEnd = `${today} 23:59`;

      // Query today's appointments for this facility, ordered by start time
      const { data: todayAppts, error: apptError } = await supabase
        .from("appointments")
        .select("apmt_patient_id, apmt_start_time, apmt_status")
        .eq("apmt_fac_id", facilityId)
        .gte("apmt_start_time", todayStart)
        .lte("apmt_start_time", todayEnd)
        .not("apmt_patient_id", "is", null)
        .not("apmt_status", "eq", "CA")  // exclude cancelled
        .order("apmt_start_time", { ascending: true })
        .limit(50);

      if (apptError) {
        console.error("[xray-patient-search] Today's appointments error:", apptError);
      }

      if (todayAppts && todayAppts.length > 0) {
        // Dedupe patient IDs while preserving appointment time order
        const seen = new Set<string>();
        const orderedIds: string[] = [];
        for (const appt of todayAppts) {
          if (!seen.has(appt.apmt_patient_id)) {
            seen.add(appt.apmt_patient_id);
            orderedIds.push(appt.apmt_patient_id);
          }
        }

        const { data, error } = await supabase
          .from("patients")
          .select(PATIENT_COLS)
          .in("id", orderedIds)
          .is("deleted_at", null);
        if (error) throw new Error("Today's patients fetch failed");
        patients = data || [];
        // Preserve appointment time order
        const idOrder = new Map(orderedIds.map((id: string, i: number) => [id, i]));
        patients.sort((a: any, b: any) => (idOrder.get(a.id) ?? 999) - (idOrder.get(b.id) ?? 999));

        console.log(`[xray-patient-search] Today: ${patients.length} patients from ${todayAppts.length} appointments`);
      }

      // Fallback: if no appointments today, return most recently added patients
      if (patients.length === 0) {
        console.log("[xray-patient-search] No appointments today — falling back to recent patients");
        const { data: recentLinks } = await supabase
          .from("patients_to_facilities")
          .select("patient_id")
          .eq("facility_id", facilityId)
          .is("deleted_at", null)
          .order("created_at", { ascending: false })
          .limit(LIMIT);

        if (recentLinks && recentLinks.length > 0) {
          const ids = recentLinks.map((l: any) => l.patient_id);
          const { data } = await supabase
            .from("patients")
            .select(PATIENT_COLS)
            .in("id", ids)
            .is("deleted_at", null);
          patients = data || [];
          const idOrder = new Map(ids.map((id: string, i: number) => [id, i]));
          patients.sort((a: any, b: any) => (idOrder.get(a.id) ?? 999) - (idOrder.get(b.id) ?? 999));
        }
      }

    // ── 1. UUID exact lookup ──
    } else if (isUuid(term)) {
      console.log("[xray-patient-search] Route: UUID lookup");
      const { data, error } = await supabase
        .from("patients")
        .select(PATIENT_COLS)
        .eq("id", term)
        .is("deleted_at", null)
        .maybeSingle();

      if (error) throw new Error("UUID lookup failed");
      if (data && await verifyFacility(supabase, data.id, facilityId)) {
        patients = [data];
      }

    // ── 2. DOB search ──
    } else {
      const dateInfo = detectDatePattern(term);

      if (dateInfo.isDate && dateInfo.isoDate) {
        console.log("[xray-patient-search] Route: DOB search");
        const { data, error } = await supabase.rpc("search_patients_by_dob_v3", {
          p_dob: dateInfo.isoDate,
          p_facility_id: facilityId,
          p_limit: LIMIT,
        });
        if (error) throw new Error("DOB search failed");
        patients = data || [];

      // ── 3. Phone search (digits only, no letters) ──
      } else if (isPhoneSearch(term)) {
        console.log("[xray-patient-search] Route: Phone search");
        const phoneDigits = term.replace(/\D/g, "");
        // Use direct query instead of RPC (RPC has auth.uid() check that fails for service role)
        const { data, error } = await supabase.rpc("search_patients_by_name_v1", {
          p_search_term: phoneDigits,
          p_facility_id: facilityId,
          p_limit: LIMIT,
        });
        // If name RPC didn't find by phone digits in name/MRN fields, search contacts directly
        if (!error && (!data || data.length === 0)) {
          // Query contacts table directly for phone match
          const { data: contactMatches } = await supabase
            .from("contactresources")
            .select("id, contactData")
            .ilike("contactData", `%${phoneDigits}%`)
            .limit(100);

          if (contactMatches && contactMatches.length > 0) {
            const contactIds = contactMatches.map((c: any) => c.id);
            const { data: junctions } = await supabase
              .from("patients_to_contactresources")
              .select("patient_id")
              .in("contactresource_id", contactIds)
              .eq("type", 1)
              .is("deleted_at", null);

            if (junctions && junctions.length > 0) {
              const patientIds = [...new Set(junctions.map((j: any) => j.patient_id))];
              // Verify these patients belong to this facility
              const { data: facilityLinks } = await supabase
                .from("patients_to_facilities")
                .select("patient_id")
                .in("patient_id", patientIds)
                .eq("facility_id", facilityId)
                .is("deleted_at", null);

              if (facilityLinks && facilityLinks.length > 0) {
                const facilityPatientIds = facilityLinks.map((fl: any) => fl.patient_id);
                const { data: patientData } = await supabase
                  .from("patients")
                  .select(PATIENT_COLS)
                  .in("id", facilityPatientIds)
                  .is("deleted_at", null)
                  .limit(LIMIT);
                patients = patientData || [];
              }
            }
          }
        } else {
          patients = data || [];
        }
        if (error) throw new Error("Phone search failed");

      // ── 4. Name / MRN search (single RPC — handles any facility size) ──
      } else {
        console.log("[xray-patient-search] Route: Name/MRN search");
        const { data, error } = await supabase.rpc("search_patients_by_name_v1", {
          p_search_term: term,
          p_facility_id: facilityId,
          p_limit: LIMIT,
        });
        if (error) {
          console.error("[xray-patient-search] Name RPC error:", error);
          throw new Error("Name search failed");
        }
        patients = data || [];
      }
    }

    // ── Empty result shortcut ──
    if (patients.length === 0) {
      return new Response(
        JSON.stringify({ patients: [] }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // ── Hydrate missing fields ──
    // RPC results (phone/DOB) already have dob, phone, email, profile_picture_url.
    // UUID/MRN/name results need hydration from related tables.
    const patientIds = patients.map((p: any) => p.id);
    const needsHydration = patients.some((p: any) => p.dob === undefined);

    let dobMap: Record<string, string | null> = {};
    let sexMap: Record<string, string | null> = {};
    let phoneMap: Record<string, string | null> = {};
    let emailMap: Record<string, string | null> = {};
    let pictureMap: Record<string, string | null> = {};

    if (needsHydration) {
      // Fetch demographics, contacts, and profile pictures in parallel
      const [demoResult, contactResult, pictureResult] = await Promise.all([
        supabase
          .from("patients_demographic")
          .select("patient_id, dob, sex")
          .in("patient_id", patientIds)
          .is("deleted_at", null),
        supabase
          .from("patients_to_contactresources")
          .select("patient_id, type, isPrimary, contactresource:contactresources(contactData)")
          .in("patient_id", patientIds)
          .is("deleted_at", null),
        supabase
          .from("patients_profile_pictures")
          .select("patient_id, picture_url")
          .in("patient_id", patientIds)
          .eq("is_active", true)
          .is("deleted_at", null),
      ]);

      (demoResult.data || []).forEach((d: any) => {
        dobMap[d.patient_id] = d.dob;
        sexMap[d.patient_id] = d.sex;
      });

      (contactResult.data || []).forEach((c: any) => {
        const contactData = c.contactresource?.contactData;
        if (!contactData) return;
        if (c.type === 1) {
          if (!phoneMap[c.patient_id] || c.isPrimary) phoneMap[c.patient_id] = contactData;
        } else if (c.type === 2) {
          if (!emailMap[c.patient_id] || c.isPrimary) emailMap[c.patient_id] = contactData;
        }
      });

      (pictureResult.data || []).forEach((p: any) => {
        pictureMap[p.patient_id] = p.picture_url ? getProxyUrl(p.picture_url, SUPABASE_URL) : null;
      });
    }

    // ── Build response ──
    const results = patients.map((p: any) => {
      const fromRpc = p.dob !== undefined || p.profile_picture_url !== undefined;

      return {
        id: p.id,
        first_name: p.first_name,
        last_name: p.last_name,
        middle_name: p.middle_name || null,
        preferred_name: p.preferred_name || null,
        medical_record_number: p.medical_record_number || null,
        dob: fromRpc ? (p.dob || null) : (dobMap[p.id] || null),
        sex: fromRpc ? (p.sex || null) : (sexMap[p.id] || null),
        phone: fromRpc ? (p.phone || null) : (phoneMap[p.id] || null),
        email: fromRpc ? (p.email || null) : (emailMap[p.id] || null),
        profile_picture_url: fromRpc
          ? (p.profile_picture_url ? getProxyUrl(p.profile_picture_url, SUPABASE_URL) : null)
          : (pictureMap[p.id] || null),
      };
    });

    console.log(`[xray-patient-search] Returning ${results.length} patients`);

    return new Response(
      JSON.stringify({ patients: results }),
      { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );

  } catch (error) {
    console.error("[xray-patient-search] Error:", error);
    return new Response(
      JSON.stringify({ error: error instanceof Error ? error.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
