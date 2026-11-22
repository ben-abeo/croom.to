package to.croom.doorsign;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class PeopleModelTest {

    // What the model gave for a street with a bus and three people in front of it (classes as COCO numbers them: 0 a person, 5 a
    // bus), surest first, as its outputs 1 and 2 hold them
    static final float[] CLASSES = {5f, 0f, 0f, 0f, 40f, 30f, 0f, 30f, 0f};
    static final float[] SCORES = {0.83f, 0.79f, 0.65f, 0.50f, 0.26f, 0.17f, 0.16f, 0.16f, 0.16f};

    @Test public void theSurestPersonIsHowSureItIs() {
        assertEquals(0.79f, PeopleModel.best(CLASSES, SCORES, 9), 0f);
    }

    @Test public void otherThingsAreNotPeopleHoweverSure() {
        assertEquals(0f, PeopleModel.best(new float[] {5f, 16f, 62f}, new float[] {0.9f, 0.8f, 0.7f}, 3), 0f);
    }

    @Test public void onlyWhatWasFoundCounts() {
        assertEquals(0f, PeopleModel.best(CLASSES, SCORES, 1), 0f);             // the rest of the arrays is left from an earlier picture
        assertEquals(0f, PeopleModel.best(CLASSES, SCORES, 0), 0f);
        assertEquals(0f, PeopleModel.best(CLASSES, SCORES, -3), 0f);
        assertEquals(0.79f, PeopleModel.best(CLASSES, SCORES, 400), 0f);        // a count past the arrays reads no further than they go
    }

    @Test public void aPersonHalfSureIsSomeone() {
        assertEquals(0.5f, PeopleModel.SURE, 0f);
    }
}
