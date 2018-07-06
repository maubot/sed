FROM maubot/plugin-base

COPY . /go/src/maubot.xyz/sed
CMD ["go", "build", "-buildmode=plugin", "-o", "/output/sed.mbp", "maubot.xyz/sed"]
