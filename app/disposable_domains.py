"""Throwaway email services that bots use to sign up. Add more in Settings → Members and community."""
DOMAINS = set("""
10minutemail.com 10minutemail.net 10minemail.com 20minutemail.com 33mail.com anonbox.net anonymbox.com
burnermail.io byom.de clrmail.com cool.fr.nf courriel.fr.nf crazymailing.com deadaddress.com despam.it
discard.email discardmail.com discardmail.de disposableemailaddresses.com disposableinbox.com dispostable.com
dodgit.com dropmail.me dumpmail.de e4ward.com email-fake.com emailfake.com emailondeck.com emailtemporanea.com
emailtemporanea.net emailtemporario.com.br emailwarden.com emltmp.com emz.net fakeinbox.com fakemail.net
fakemailgenerator.com fastacura.com filzmail.com getairmail.com getnada.com gishpuppy.com guerrillamail.biz
guerrillamail.com guerrillamail.de guerrillamail.info guerrillamail.net guerrillamail.org guerrillamailblock.com
harakirimail.com hide.biz.st hidemail.de incognitomail.com incognitomail.org inboxalias.com inboxbear.com
inboxkitten.com jetable.com jetable.fr.nf jetable.net jetable.org kasmail.com killmail.com klzlk.com
linshiyouxiang.net lroid.com mail-temp.com mail.tm mail7.io mailcatch.com maildrop.cc mailexpire.com
mailforspam.com mailimate.com mailinator.com mailinator.net mailinator.org mailinator2.com mailmetrash.com
mailmoat.com mailnesia.com mailnull.com mailpoof.com mailsac.com mailshell.com mailtemp.info mailtothis.com
meltmail.com mintemail.com moakt.com mohmal.com mt2014.com mytemp.email mytrashmail.com nada.email
nospam.ze.tc nowmymail.com objectmail.com one-time.email oneoffemail.com owlymail.com pookmail.com proxymail.eu
rcpt.at rmqkr.net selfdestructingmail.com sharklasers.com shitmail.me shortmail.net sneakemail.com
sofimail.com spam4.me spambog.com spambox.us spamdecoy.net spamex.com spamfree24.org spamgourmet.com spamhole.com
spaml.com spammotel.com spamspot.com spamthis.co.uk spamthisplease.com supermailer.jp tafmail.com teleworm.us
temp-mail.io temp-mail.org temp-mails.com tempail.com tempemail.co tempemail.com tempemail.net tempinbox.com
tempmail.com tempmail.dev tempmail.net tempmail.plus tempmailaddress.com tempmailo.com tempomail.fr
temporarily.de temporaryemail.net temporaryforwarding.com temporaryinbox.com throwam.com throwawayemailaddress.com
throwawaymail.com tmail.ws tmailor.com tmpmail.net tmpmail.org trash-mail.com trash-mail.de trash2009.com
trashdevil.com trashemail.de trashmail.at trashmail.com trashmail.de trashmail.me trashmail.net trashmail.org
trashymail.com trbvm.com tyldd.com uroid.com wegwerfemail.de wegwerfmail.de wegwerfmail.net wh4f.org
yepmail.net yopmail.com yopmail.fr yopmail.net zetmail.com zoemail.org
""".split())


def is_disposable(email, extra=()):
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    blocked = DOMAINS | {d.strip().lower().lstrip("@") for d in extra if d.strip()}
    parts = domain.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))
