"""The Laptop switch page's camera: chosen from the USB cameras on the Pi, typed when there is none."""
from croom.settings.devices import USB_CAMERA, offer_usb_camera

FIELDS = [{"path": "laptop.gpio_pin", "kind": "number"}, {"path": USB_CAMERA, "kind": "text"}]


def test_the_cameras_on_the_pi_are_offered_with_the_configs_own():
    found = {USB_CAMERA: [("046d:0881", "Logi Rally Camera (046d:0881)")]}
    field = offer_usb_camera(FIELDS, {USB_CAMERA: "046d:0866"}, found)[1]
    assert field["kind"] == "choice" and [c["value"] for c in field["choices"]] == ["046d:0881", "046d:0866"]
    assert field["choices"][1]["label"] == "046d:0866 (not connected now)" and FIELDS[1]["kind"] == "text"


def test_with_no_camera_on_the_pi_the_id_is_typed_as_before():
    assert offer_usb_camera(FIELDS, {USB_CAMERA: "046d:0866"}, {USB_CAMERA: []}) is FIELDS
    assert offer_usb_camera(FIELDS, {USB_CAMERA: "046d:0866"}, None) is FIELDS
