"""MCP-server: `vigilo-mcp`.

Eksponerer Vigilos foreldre-API som verktøy for agenten. `api_get` er
kartleggingsverktøyet: rått GET mot vilkårlig sti bak gatewayen, så nye
endepunkter kan utforskes rett fra agenten før de blir egne verktøy.
"""

import functools
import json
from typing import Annotated

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import SearchIncomplete, VigiloClient

from .output import sanitize as _sanitize, thread_summary as _thread_summary, verified_child_context


def _json(value, **kwargs):
    return json.dumps(_sanitize(value), **kwargs)


def _anticipated_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, SearchIncomplete) as error:
            raise ToolError(_sanitize(str(error))) from None
    return wrapper



_client: VigiloClient | None = None


def client() -> VigiloClient:
    global _client
    if _client is None:
        _client = VigiloClient()
    return _client


def use_client(c: VigiloClient) -> None:
    """Bruk en bestemt klient i verktøyene (f.eks. med egen TokenStore i gatewayen)."""
    global _client
    _client = c


def list_children(force_refresh: bool = False) -> str:
    """List barna knyttet til den innloggede foresatte, med childId.

    Leseresponsar kan vere opptil 60 sekund gamle. force_refresh=true hentar på nytt."""
    return _json(client().get_children(force_refresh=force_refresh), ensure_ascii=False, indent=2)


def list_message_threads(
    child_id: str, page_size: Annotated[int, Field(ge=1, le=20)] = 20, page_id: int | None = None,
    include_after_school: bool = True, force_refresh: bool = False,
) -> str:
    """En kompakt side ordinære meldingstråder med pageId og hasMore.

    Leseresponsar kan vere opptil 60 sekund gamle. force_refresh=true hentar på nytt.

    Bruk page_id for flere sider. Åpne hver tråd for hele meldingsinnholdet;
    Listen viser inntil 200 tegn av siste melding. hasMore betyr full side;
    neste side kan være tom. Standard sidestørrelse er 20. Inkluderer SFO som standard.
    """
    if type(page_size) is not int or not 1 <= page_size <= 20:
        raise ValueError("page_size må vere eit heiltal frå 1 til 20 for trådlista.")
    with client().fresh_reads(force_refresh):
        page = client().get_message_threads_page(child_id, page_size, include_after_school, page_id)
    threads = page.get("messageThreads", [])
    summary = {key: value for key, value in page.items() if key != "messageThreads"}
    summary.update({"messageThreads": [_thread_summary(thread) for thread in threads],
                    "hasMore": len(threads) == page_size})
    if len(threads) == page_size and type(page.get("pageId")) is int:
        summary["nextPageId"] = page["pageId"] + 1
    return _json(summary, ensure_ascii=False, indent=2)


def _verified_child(thread_uid: str, child_id: str, force_refresh: bool):
    before = client().cached_reads()
    with client().fresh_reads(force_refresh):
        try:
            return verified_child_context(client(), thread_uid, child_id)
        except ValueError:
            if client().cached_reads() == before:
                raise  # Every list read was live; retrying would only repeat it.
    # Cached lists may predate a new thread; confirm the miss against fresh lists.
    with client().fresh_reads():
        return verified_child_context(client(), thread_uid, child_id)


def get_message_thread(
    thread_uid: str, page_size: int = 50, page_id: int | None = None,
    child_id: str | None = None, force_refresh: bool = False,
) -> str:
    """En side meldinger med vedlegg og svar-/slettestatus, uten å markere lest.

    Meldingane blir alltid henta ferske frå Vigilo.

    child_id verifiserer barnet frå barnelista og trådens medlemskap via barnets
    paginerte trådliste. Lister kan vere opptil 60 sekund gamle; ved manglande
    treff blir dei henta på nytt før feil. force_refresh=true hentar listene ferske
    med ein gong. Utan child_id blir det tvetydige child-feltet utelate.
    For eldre meldinger: bruk returnert pageId + 1 som page_id inntil tom side.
    """
    selected = None
    if child_id is not None:
        selected = _verified_child(thread_uid, child_id, force_refresh)
    detail = dict(client().get_thread(thread_uid, page_size, page_id))
    if selected is not None and detail.get("threadUid") != thread_uid:
        raise ValueError("Detaljresponsen samsvarar ikkje med valt tråd.")
    detail.pop("child", None)
    if selected is not None:
        detail["child"] = selected
    return _json(detail, ensure_ascii=False, indent=2)


def search_message_thread(
    thread_uid: str, query: str, page_size: int = 50, max_pages: int = 100,
    timeout_s: float = 45.0,
) -> str:
    """Søk i alle meldingers tekst og vedleggsnavn i én ordinær tråd.

    Bokstavelig søk uten skille på store/små bokstaver. Henter eldre sider
    til tom side, uten å markere lest. Søker ikke inne i selve vedleggsfilene.
    Ved sidegrense eller ugyldig paging gis feil, ikke et ufullstendig treffsett.
    """
    return _json(client().search_message_thread(
        thread_uid, query, page_size, max_pages, timeout_s,
    ), ensure_ascii=False, indent=2)


def message_participants(thread_uid: str, child_id: str, force_refresh: bool = False) -> str:
    """Deltakere og trådstarter for en ordinær tråd og valgt barn.

    Leseresponsar kan vere opptil 60 sekund gamle. force_refresh=true hentar på nytt."""
    with client().fresh_reads(force_refresh):
        return _json(client().message_participants(thread_uid, child_id),
                          ensure_ascii=False, indent=2)


def list_pickup_notes(child_id: str, month: str | None = None, day: str | None = None) -> str:
    """Hentemeldingar (SFO-hentebeskjedar) for eit barn i éin kalendermånad.

    month er "YYYY-MM" (standard: inneverande månad i Europe/Oslo). day
    ("YYYY-MM-DD") viser berre meldingar som gjeld den dagen, med active=true på
    den som gjeld (sist endra, som i appen). typeAbbreviation er appens kode
    (t.d. GHS). Vigilo viser berre kvardagar i kalenderen. Gir òg barnets
    SFO-einingar (afterSchoolUnits). Utan SFO-eining blir ingenting henta frå Vigilo.
    Berre lesing; canUpdate/canDelete viser kva appen ville tillate.
    """
    return _json(client().pickup_notes(child_id, month, day), ensure_ascii=False, indent=2)


def pickup_note_options(child_id: str, organizational_unit_id: str) -> str:
    """Kategoriar, meldingstypar og registreringsfristar for hentemeldingar.

    organizational_unit_id må vere ei SFO-eining frå afterSchoolUnits i
    list_pickup_notes. Berre lesing.
    """
    return _json(client().pickup_note_options(child_id, organizational_unit_id),
                 ensure_ascii=False, indent=2)


def create_pickup_note(
    child_id: str, organizational_unit_id: str, type_id: str, from_date: str,
    to_date: str, time_of_day: str, note_text: str | None = None,
    repeat_days: list[str] | None = None, every_other_week: bool = False,
) -> str:
    """Opprett ei hentemelding (SFO) når brukaren har bestilt den konkrete handlinga.

    Sjekk først eksisterande meldingar med list_pickup_notes og vel type_id frå
    pickup_note_options (aktiv type med categoryEnabled). Datoar YYYY-MM-DD, begge
    dagar inkludert. time_of_day "HH:MM" i kvarter mellom 06:00 og 18:00.
    note_text er påkravd når typen har useComment=true. repeat_days (monday–friday)
    gir vekentleg gjentaking i perioden; every_other_week=true gir annakvar veke.
    Utan repeat_days gjeld meldinga kvar dag i perioden. Ved ukjent resultat:
    kontroller list_pickup_notes før nytt forsøk, aldri automatisk retry.
    """
    return _json(client().create_pickup_note(
        child_id, organizational_unit_id, type_id, from_date, to_date, time_of_day,
        note_text, repeat_days, every_other_week,
    ), ensure_ascii=False, indent=2)


def update_pickup_note(
    child_id: str, note_id: str, month: str, type_id: str | None = None,
    from_date: str | None = None, to_date: str | None = None,
    time_of_day: str | None = None, note_text: str | None = None,
    repeat_days: list[str] | None = None, every_other_week: bool | None = None,
) -> str:
    """Endre ei hentemelding (SFO) når brukaren har bestilt den konkrete endringa.

    Endringa gjeld heile meldinga/serien; Vigilo har ingen endring av éin dag.
    note_id og month ("YYYY-MM", ein månad meldinga er med i) frå list_pickup_notes.
    Krev canUpdate=true; elles kan berre sluttdatoen endrast med
    set_pickup_note_end_date. Utelatne felt beheld verdien. Datoar YYYY-MM-DD med
    begge dagar inkludert; time_of_day "HH:MM" i kvarter 06:00–18:00.
    repeat_days (monday–friday) set vekedagane; repeat_days=[] slår av gjentaking.
    Ved ukjent resultat: kontroller list_pickup_notes, aldri automatisk retry.
    """
    return _json(client().update_pickup_note(
        child_id, note_id, month, type_id, from_date, to_date, time_of_day, note_text,
        repeat_days, every_other_week,
    ), ensure_ascii=False, indent=2)


def set_pickup_note_end_date(child_id: str, note_id: str, month: str, last_date: str) -> str:
    """Set ny siste dag (YYYY-MM-DD) for ei hentemelding/serie etter bestilling.

    Krev canUpdateToDate=true (t.d. seriar som alt har starta). note_id og month
    frå list_pickup_notes. Kontroller resultatet med list_pickup_notes etterpå;
    ved ukjent resultat aldri automatisk retry.
    """
    return _json(client().set_pickup_note_end_date(child_id, note_id, month, last_date),
                 ensure_ascii=False, indent=2)


def delete_pickup_note(child_id: str, note_id: str, month: str) -> str:
    """Slett heile hentemeldinga/serien når brukaren har bestilt det.

    Krev canDelete=true. note_id og month frå list_pickup_notes. Ved ukjent
    resultat: kontroller list_pickup_notes, aldri automatisk retry.
    """
    return _json(client().delete_pickup_note(child_id, note_id, month),
                 ensure_ascii=False, indent=2)


def read_message_attachments(thread_uid: str) -> str:
    """Last ned og les vedleggene i en meldingstråd (f.eks. ukeplan-PDF).

    Returnerer utdratt tekst per vedlegg. Bruk dette når en melding har vedlegg
    (hasAttachments/attachments) og du trenger innholdet — typisk ukeplanen med
    lekser, turer og «ta med»-beskjeder som ofte legges ved som PDF.
    """
    return _json(
        client().read_thread_attachments(thread_uid), ensure_ascii=False, indent=2
    )


def read_post_attachments(post_id: str) -> str:
    """Last ned og les vedleggene i et oppslag/«Siste nytt»-post (PDF/docx→tekst).

    Ukeplanen legges typisk ut som et oppslag med et fler-ukers PDF-vedlegg
    (f.eks. «uke 36-38»), IKKE som en meldingstråd. Bruk `id`-feltet fra
    `news_feed` som post_id. Returnerer utdratt tekst per vedlegg.
    """
    return _json(client().read_post_attachments(post_id), ensure_ascii=False, indent=2)


def api_get(path: str, params_json: str = "{}") -> str:
    """Rått GET mot foreldre-API-et — for kartlegging av nye endepunkter.

    path: f.eks. "/api/children". params_json: query-parametre som JSON-objekt;
    userId-query fjernes før sending, slik Android-appen gjør. Returnerer status og body
    uansett utfall, så 404/400 også gir informasjon.
    """
    params = json.loads(params_json)
    r = client().get(path, params=params)
    body = _sanitize(r.text)
    try:
        body = _json(r.json(), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    return f"HTTP {r.status_code}\n{body[:20000]}"


# --- Web-foreldreportal -------------------------------------------------
# Timeplan, fravær, samtykke, nyheter m.m. som ikke finnes i app-gatewayen.
# childId og organizationalUnitId hentes fra web_list_children.


def web_list_children() -> str:
    """List én rad per barn/enhet med childId, organizationalUnitId og type.

    organizationalUnitId (skole/klasse) trengs til timeplan, samtykke og
    vurdering; childId brukes overalt. Gir også skole- og gruppenavn.
    """
    return _json(client().children(), ensure_ascii=False, indent=2)


def news_feed(child_id: str, from_date: str = "", to_date: str = "") -> str:
    """Oppslag/«Siste nytt» for et barn. Datoer YYYY-MM-DD (default: siste 30 dager)."""
    return _json(
        client().news_feed(child_id, from_date or None, to_date or None),
        ensure_ascii=False, indent=2,
    )


def absences(child_id: str) -> str:
    """Fravær for et barn."""
    return _json(client().absences(child_id), ensure_ascii=False, indent=2)


def consent_forms(child_id: str, organizational_unit_id: str) -> str:
    """Samtykkeskjemaer for et barn (krever organizationalUnitId)."""
    return _json(
        client().consent_forms(child_id, organizational_unit_id), ensure_ascii=False, indent=2
    )


def timetable(child_id: str, organizational_unit_id: str, week: str = "") -> str:
    """Timeplan/ukesplan for en ISO-uke ("YYYY-WW", default inneværende uke)."""
    return _json(
        client().lessons(child_id, organizational_unit_id, week or None),
        ensure_ascii=False, indent=2,
    )


def scheduling_events(child_id: str, organizational_unit_id: str, week: str = "") -> str:
    """Timeplan-hendelser (prøver, aktiviteter) for en ISO-uke ("YYYY-WW")."""
    return _json(
        client().scheduling_events(child_id, organizational_unit_id, week or None),
        ensure_ascii=False, indent=2,
    )


def web_api_get(path: str, params_json: str = "{}") -> str:
    """Rått GET mot web-foreldreportalen — for kartlegging av nye web-endepunkter.

    path: f.eks. "/api/absences". params_json: query-parametre som JSON-objekt.
    Ingen userId legges til (web-API-et bruker childIds/organizationalUnitId).
    Returnerer status og body uansett utfall.
    """
    r = client().web_get(path, params=json.loads(params_json))
    body = _sanitize(r.text)
    try:
        body = _json(r.json(), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    return f"HTTP {r.status_code}\n{body[:20000]}"


def message_contacts(child_id: str, organizational_unit_id: str) -> str:
    """Hent kontaktlisten for barn/skole. recipient inneholder type og externalId.

    Velg individuelle mottakere eksplisitt før meldingssending eller skolefravær.
    """
    return _json(client().message_contacts(child_id, organizational_unit_id),
                      ensure_ascii=False, indent=2)


def send_message(
    child_id: str, organizational_unit_id: str, title: str, body: str,
    recipients: list[dict[str, str]],
) -> str:
    """Send ny melding når brukeren har bestilt konkret innhold og mottakere.

    Hent message_contacts først. Minst én ansatt kreves; foresatte kan inkluderes.
    Ingen vedlegg. Ved ukjent resultat: sjekk meldingstrådene før nytt forsøk.
    """
    return _json(client().send_message(
        child_id, organizational_unit_id, title, body, recipients,
    ), ensure_ascii=False, indent=2)


def reply_message(thread_uid: str, body: str) -> str:
    """Send bestilt svar i en ordinær meldingstråd fra get_message_thread.

    Ingen vedlegg eller fraværstråder. Ved ukjent resultat: les tråden før nytt forsøk.
    """
    return _json(client().reply_message(thread_uid, body), ensure_ascii=False, indent=2)


def register_student_absence(
    child_id: str, organizational_unit_id: str, from_date: str, to_date: str,
    note: str, title: str, recipients: list[dict[str, str]],
) -> str:
    """Registrer skolefravær når brukeren har bestilt den konkrete registreringen.

    Datoer YYYY-MM-DD, inkludert begge dager. Krever tittel, begrunnelse og
    eksplisitt valgte recipient-objekter fra message_contacts. Ingen vedlegg.
    Ved ukjent resultat: kontroller fravær før nytt forsøk, aldri automatisk retry.
    """
    return _json(client().register_student_absence(
        child_id, organizational_unit_id, from_date, to_date, note, title, recipients,
    ), ensure_ascii=False, indent=2)


def absence_codes(organizational_unit_id: str) -> str:
    """Hent aktive fraværskoder for en barnehage, f.eks. Fri, Ferie og Syk.

    Bruk id fra resultatet som absence_code_id i register_childcare_absence.
    """
    return _json(client().absence_codes(organizational_unit_id), ensure_ascii=False, indent=2)


def register_childcare_absence(
    child_id: str, organizational_unit_id: str, from_date: str, to_date: str,
    absence_code_id: str, note: str | None = None,
) -> str:
    """Registrer barnehagefravær når brukeren har bestilt den konkrete handlingen.

    Datoer YYYY-MM-DD, begge dager inkludert. Krever aktiv absence_code_id fra
    absence_codes for barnets barnehage; note er valgfri. Ingen tittel/mottakere.
    Kontroller eksisterende fravær før sending for å unngå overlapp. Ved ukjent
    resultat: kontroller fravær før nytt forsøk, aldri automatisk retry.
    Bruk register_student_absence for skolefravær.
    """
    return _json(client().register_childcare_absence(
        child_id, organizational_unit_id, from_date, to_date, absence_code_id, note,
    ), ensure_ascii=False, indent=2)


TOOLS = (
    send_message,
    reply_message,
    absence_codes,
    register_childcare_absence,
    message_contacts,
    register_student_absence,
    list_children,
    list_message_threads,
    message_participants,
    search_message_thread,
    get_message_thread,
    list_pickup_notes,
    pickup_note_options,
    create_pickup_note,
    update_pickup_note,
    set_pickup_note_end_date,
    delete_pickup_note,
    read_message_attachments,
    read_post_attachments,
    api_get,
    web_list_children,
    news_feed,
    absences,
    consent_forms,
    timetable,
    scheduling_events,
    web_api_get,
)


def register_tools(server: MCPServer) -> None:
    """Registrer alle verktøyene på en MCP-server.

    Brukes både av stdio-serveren under og av remote-gatewayen
    (`vigilo_connector.gateway`), så verktøyene defineres ett sted.
    """
    for fn in TOOLS:
        server.add_tool(_anticipated_errors(fn))


mcp = MCPServer("vigilo")
register_tools(mcp)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
