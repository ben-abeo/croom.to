package to.croom.doorsign;

/** A job that can't be done, or did not work: the words say why, for the person reading Settings on the touch Pi. */
final class Refused extends Exception {
    Refused(String words) {
        super(words);
    }

    String words() {
        return getMessage();
    }
}
