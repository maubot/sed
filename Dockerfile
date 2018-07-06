FROM maubot/plugin-base

COPY . /go/src/maubot.xyz/sed
RUN go build -buildmode=plugin -o /maubot-plugins/sed.mbp maubot.xyz/sed

FROM scratch
COPY --from=builder /maubot-plugins/sed.mbp /maubot-plugins/sed.mbp
VOLUME /output
CMD ["cp", "/maubot-plugins/sed.mbp", "/output/sed.mbp"]
