FROM maubot/plugin-base

COPY . /go/src/maubot.xyz/sed
RUN go build -buildmode=plugin -o /maubot-plugins/sed.mbp maubot.xyz/sed

FROM scratch
VOLUME /output
COPY --from=builder /maubot-plugins/sed.mbp /output/sed.mbp
# CMD ["cp", "/maubot-plugins/sed.mbp", "/output/sed.mbp"]
