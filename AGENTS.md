# Personvern i repoet

Arbeid direkte på `main`. Ikke opprett eller bruk arbeidsbrancher.
Verifiser endringer lokalt med relevante kontroller før commit og push.
Push først når kontrollene passerer og brukeren har bestilt push.

Bruk kun syntetiske navn og ID-er i kode, tester og eksempler. Ikke kopier
navn på faktiske barn, andre personopplysninger, tokens eller API-data fra
live-oppslag til repoet eller commit-meldinger.

Dette omfatter private opplysninger om familie, skole, barnehage og kommune,
samt miljødetaljer som faktiske Tailscale-navn, tailnet-domener, vertsnavn,
IP-adresser og interne URL-er. Bruk syntetiske plassholdere i dokumentasjon
og testdata, og hold lokal konfigurasjon og credentials utenfor repoet.
Offentlige repo-adresser, forfattermetadata og påkrevde lisensopplysninger
kan beholdes; private person- og miljøopplysninger skal ikke inn.

Kontroller nye og endrede filer samt commit-meldingen før commit. Testdata
skal bruke tydelige plassholdere som «Testbarn A» og «Testavdeling».

Sjekk appens valideringsregler før nye skrivehandlinger implementeres eller
utføres. HTTP-suksess alene beviser ikke at appens validering er fulgt.

Bruk eksplisitte filnavn ved staging, aldri `git add -A`. Signer commits med
`git commit -s`. Ingen agentreferanser, emojis eller co-author-attribusjon.
